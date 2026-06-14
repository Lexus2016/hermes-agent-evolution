#!/usr/bin/env python3
"""Evolution watchdog — deterministic pipeline health check (issue 83).

Runs as a ``no_agent`` cron job (no LLM involved): scans the evolution
pipeline's observable state and prints a short alert report to stdout when
something is wrong. The cron scheduler delivers non-empty stdout to the
owner's configured messaging channels; EMPTY stdout means "all healthy"
and nothing is delivered (silent run).

Checks
------
1. Stage reports — every daily evolution stage must have left a non-trivial
   report file for its most recent *expected* slot (slot + grace period).
   This catches jobs that were killed mid-run without any record (the
   2026-06-10 gateway-restart incident) and jobs that finished "ok" while
   producing nothing.
2. Job registry health — evolution jobs in ``cron/jobs.json``: last run
   status is not "error", the job actually ran within its cadence window,
   and it is not stuck in a running state for hours.
3. GitHub access — ``gh auth status`` works and the API rate limit isn't
   nearly exhausted (every pipeline stage depends on gh).

Designed to be import-safe for tests: all checks are pure functions taking
explicit paths / clocks / runners.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Dict, List, Tuple

# stage name -> (daily slot hour, report file extension)
# Slots and extensions mirror cron/evolution/*.yaml (schedule + output.file).
# Drift is locked down by TestStagesMirrorCronSpecs in
# tests/scripts/test_evolution_watchdog.py.
STAGES: Dict[str, Tuple[int, str]] = {
    "research": (9, "md"),
    "introspection": (20, "json"),
    "analysis": (21, "json"),
    "implementation": (22, "md"),
    "integration": (23, "json"),
}

GRACE_HOURS = 2
MIN_REPORT_BYTES = 50
DAILY_STALE_HOURS = 26
WEEKLY_STALE_HOURS = 8 * 24
STUCK_RUNNING_HOURS = 12
MIN_GH_RATE_REMAINING = 200

# Jobs that are weekly, not daily (looser staleness threshold).
WEEKLY_JOBS = {"evolution-upstream-sync"}
# The watchdog itself must not alert about its own first run.
SELF_NAMES = {"evolution-watchdog"}


def expected_report_date(now: datetime, slot_hour: int, grace_hours: int = GRACE_HOURS) -> str:
    """Date (YYYY-MM-DD) whose report should exist for a daily slot.

    If the slot (plus grace) has already passed today, today's report is
    expected; otherwise yesterday's is the most recent one that must exist.
    """
    slot_deadline = now.replace(hour=slot_hour, minute=0, second=0, microsecond=0) + timedelta(
        hours=grace_hours
    )
    day = now.date() if now >= slot_deadline else (now - timedelta(days=1)).date()
    return day.isoformat()


def check_stage_reports(evolution_dir: Path, now: datetime) -> List[str]:
    """Alert for every stage whose expected report is missing or trivial."""
    alerts: List[str] = []
    for stage, (slot_hour, ext) in STAGES.items():
        date = expected_report_date(now, slot_hour)
        report = evolution_dir / stage / f"{date}.{ext}"
        if not report.exists():
            alerts.append(
                f"stage '{stage}': expected report {report.name} is MISSING "
                f"(slot {slot_hour:02d}:00 + {GRACE_HOURS}h grace passed; "
                f"the job died without record or never ran)"
            )
            continue
        try:
            size = report.stat().st_size
        except OSError:
            size = 0
        if size < MIN_REPORT_BYTES:
            alerts.append(
                f"stage '{stage}': report {report.name} is suspiciously small "
                f"({size} bytes) — the cycle likely produced nothing"
            )
    return alerts


def _parse_iso(value) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value)).replace(tzinfo=None)
    except ValueError:
        return None


def check_jobs(jobs_file: Path, now: datetime) -> List[str]:
    """Alert on unhealthy evolution job records in the cron registry."""
    alerts: List[str] = []
    try:
        data = json.loads(jobs_file.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return [f"cron registry {jobs_file} unreadable: {exc}"]

    jobs = data.get("jobs", data if isinstance(data, list) else [])
    for job in jobs:
        name = str(job.get("name", ""))
        if not name.startswith("evolution-") or name in SELF_NAMES:
            continue
        if not job.get("enabled", True):
            continue

        if job.get("last_status") == "error":
            alerts.append(
                f"job '{name}': last run FAILED — {job.get('last_error') or 'no error text'}"
            )

        stale_hours = WEEKLY_STALE_HOURS if name in WEEKLY_JOBS else DAILY_STALE_HOURS
        last_run = _parse_iso(job.get("last_run_at"))
        if last_run is None:
            created = _parse_iso(job.get("created_at"))
            if created is not None and now - created <= timedelta(hours=stale_hours):
                # Freshly (re)registered job — its first slot hasn't come yet.
                # Re-registration wipes run history; alerting here is noise.
                continue
            alerts.append(f"job '{name}': has never recorded a run")
        elif now - last_run > timedelta(hours=stale_hours):
            alerts.append(
                f"job '{name}': last run {last_run.isoformat()} is stale "
                f"(>{stale_hours}h ago — interrupted without record, or scheduler down?)"
            )

        # Forward-compatible with the interrupted-job marker (issue 105):
        # a job stuck in 'running' state for many hours is dead.
        if job.get("state") == "running":
            started = _parse_iso(job.get("run_started_at"))
            if started and now - started > timedelta(hours=STUCK_RUNNING_HOURS):
                alerts.append(
                    f"job '{name}': marked running since {started.isoformat()} "
                    f"(>{STUCK_RUNNING_HOURS}h) — stuck or killed mid-run"
                )
    return alerts


def _default_runner(cmd: List[str]) -> Tuple[int, str]:
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def check_gh(runner: Callable[[List[str]], Tuple[int, str]] = _default_runner) -> List[str]:
    """Alert when gh auth is broken or API rate budget is nearly gone."""
    alerts: List[str] = []
    try:
        rc, _out = runner(["gh", "auth", "status"])
        if rc != 0:
            alerts.append("gh auth status FAILED — every pipeline stage depends on gh")
    except Exception as exc:  # noqa: BLE001 — any spawn failure is an alert
        return [f"gh unavailable: {exc}"]

    try:
        rc, out = runner(["gh", "api", "rate_limit"])
        if rc == 0:
            remaining = (
                json.loads(out).get("resources", {}).get("core", {}).get("remaining")
            )
            if remaining is not None and remaining < MIN_GH_RATE_REMAINING:
                alerts.append(
                    f"GitHub API rate budget nearly exhausted: {remaining} requests left"
                )
        else:
            alerts.append("gh api rate_limit failed — cannot verify API budget")
    except Exception as exc:  # noqa: BLE001
        alerts.append(f"gh rate-limit check failed: {exc}")
    return alerts


def check_health(evolution_dir: Path) -> List[str]:
    """Alert when the longitudinal health sidecar reports degraded calibration.

    evolution_metrics writes ``evolution-health.txt`` ending in ``| healthy`` when
    fine, or ``| <FLAGS>`` (LOW_SUCCESS / LOW_SELECTION_EFFICIENCY) when the
    pipeline is selecting more than it can land / rarely merging. This is what
    makes the measurement spine ACTIONABLE instead of write-only: the owner gets
    pinged when the pipeline's own health degrades. Silent when healthy or when
    there is no sidecar yet (the stage-report checks already cover 'funnel didn't
    run')."""
    try:
        line = (evolution_dir / "evolution-health.txt").read_text(encoding="utf-8").strip()
    except OSError:
        return []
    if not line or line.endswith("| healthy"):
        return []
    return [f"pipeline health degraded: {line}"]


def main() -> int:
    hermes_home = Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes")))
    evolution_dir = Path(
        os.environ.get(
            "EVOLUTION_PROFILE_DIR",
            str(hermes_home / "profiles" / "user1" / "evolution"),
        )
    )
    jobs_file = hermes_home / "cron" / "jobs.json"
    # Stage reports are dated in Hermes' CONFIGURED timezone (hermes_time.now),
    # which may differ from the server's local zone — around midnight a naive
    # datetime.now() would then look for the wrong report date. The scheduler
    # runs this script from HERMES_HOME/scripts (outside the repo's sys.path),
    # so hermes_time may not be importable — fall back to server-local wall
    # time, which is identical whenever no explicit timezone is configured.
    try:
        from hermes_time import now as _hermes_now

        now = _hermes_now().replace(tzinfo=None)
    except ImportError:
        now = datetime.now()

    alerts: List[str] = []
    alerts += check_stage_reports(evolution_dir, now)
    alerts += check_jobs(jobs_file, now)
    alerts += check_gh()
    alerts += check_health(evolution_dir)

    if alerts:
        print("🐶 Evolution watchdog — pipeline anomalies detected:")
        for a in alerts:
            print(f"  • {a}")
        print(
            f"\n(checked {len(STAGES)} stage reports, cron registry, gh access "
            f"at {now.isoformat(timespec='seconds')})"
        )
    # Empty stdout = healthy = silent run (scheduler delivers nothing).
    return 0


if __name__ == "__main__":
    sys.exit(main())
