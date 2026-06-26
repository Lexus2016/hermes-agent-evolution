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
    # analysis/implementation/integration run every 4h (processing throughput);
    # the slot here is the FIRST daily slot — the watchdog only needs "a report
    # for today exists by then", and reports are date-keyed (overwritten each
    # run). Mirrors cron/evolution/*.yaml first hour (locked by the mirror test).
    "analysis": (1, "json"),
    "implementation": (2, "md"),
    "integration": (3, "json"),
}

GRACE_HOURS = 2
MIN_REPORT_BYTES = 50
DAILY_STALE_HOURS = 26
WEEKLY_STALE_HOURS = 8 * 24
STUCK_RUNNING_HOURS = 12
MIN_GH_RATE_REMAINING = 200
# Alert when the fork falls this far behind upstream — the autonomous
# upstream-sync's own auto-merge ceiling. Past it the daily sync escalates
# (files an [UPSTREAM] issue) instead of merging, and without this check the
# fork silently accumulates a backlog for days (2026-06-19 → 301 behind).
UPSTREAM_BEHIND_ALERT = 80

# Jobs that are weekly, not daily (looser staleness threshold).
WEEKLY_JOBS = {"evolution-upstream-sync"}
# The watchdog itself must not alert about its own first run.
SELF_NAMES = {"evolution-watchdog"}

# Edge-triggering for the steady-state HEALTH alerts ------------------------
# Re-reminder cadence: a health condition that persists UNCHANGED for at least
# this many days is re-announced once (a "still unresolved" nudge) so a real
# fault can never be silenced forever by suppression. The clock resets on every
# actual emission (first sighting, transition, or a prior re-reminder).
EDGE_COOLDOWN_DAYS = 7
# State file lives beside the health sidecars (same evolution_dir resolution the
# health checks already use), so a single EVOLUTION_PROFILE_DIR override moves
# both. Small JSON: {"signature": str, "last_emitted_at": ISO8601}.
ALERT_STATE_FILENAME = "watchdog-alert-state.json"


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


def _load_jobs(jobs_file: Path | None) -> List[dict]:
    if jobs_file is None:
        return []
    try:
        data = json.loads(jobs_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return data.get("jobs", data if isinstance(data, list) else [])


def _stage_ran_clean_for_slot(jobs: List[dict], stage: str, date: str, slot_hour: int) -> bool:
    """True when the cron job for ``stage`` ran with ``last_status == "ok"`` at or
    after its slot on ``date``. A MISSING report is only a death if the job did
    NOT run clean for the slot: when it did, the stage executed and simply had
    nothing to do (e.g. analysis selected 0 → implementation/integration are
    legitimately idle and need not emit a report). That is a clean idle cycle,
    not 'the job died without record'."""
    name = f"evolution-{stage}"
    for job in jobs:
        if str(job.get("name", "")) != name:
            continue
        if job.get("last_status") != "ok":
            return False
        last_run = _parse_iso(job.get("last_run_at"))
        if last_run is None:
            return False
        try:
            slot_dt = datetime.fromisoformat(date).replace(hour=slot_hour)
        except ValueError:
            return False
        return last_run >= slot_dt
    return False


def check_stage_reports(
    evolution_dir: Path, now: datetime, jobs_file: Path | None = None
) -> List[str]:
    """Alert for every stage whose expected report is missing or trivial.

    A missing report is suppressed (not a death) when the stage's cron job ran
    clean for the slot — see ``_stage_ran_clean_for_slot``."""
    alerts: List[str] = []
    jobs = _load_jobs(jobs_file)
    for stage, (slot_hour, ext) in STAGES.items():
        date = expected_report_date(now, slot_hour)
        report = evolution_dir / stage / f"{date}.{ext}"
        if not report.exists():
            if _stage_ran_clean_for_slot(jobs, stage, date, slot_hour):
                continue  # ran clean, nothing to do — idle cycle, not a death
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


def _resolve_repo_dir() -> Path | None:
    """Locate the git repo to inspect for upstream lag.

    The watchdog runs as a no_agent script copied to HERMES_HOME/scripts, i.e.
    OUTSIDE the repo, so we resolve the repo explicitly: an env override, then
    the in-tree location (when run from the repo), then the common server
    install / agent-clone paths. Returns None when none is a git repo — the
    caller then skips the check silently.
    """
    candidates = [
        os.environ.get("EVOLUTION_REPO_DIR"),
        str(Path(__file__).resolve().parent.parent),  # scripts/ -> repo root (in-tree)
        "/usr/local/lib/hermes-agent",
        str(Path.home() / "hermes-agent-evolution"),
    ]
    for cand in candidates:
        if cand and (Path(cand) / ".git").exists():
            return Path(cand)
    return None


def check_upstream_lag(
    runner: Callable[[List[str]], Tuple[int, str]] = _default_runner,
    repo_dir: Path | None = None,
) -> List[str]:
    """Alert when the fork is too far behind upstream (sync stuck).

    The daily upstream-sync can run "ok" every day yet never MERGE — once a
    core conflict appears it escalates (files an [UPSTREAM] issue) and the fork
    falls further behind each day. ``check_jobs`` only sees the job ran, not
    that nothing landed. This check reads the real distance to ``upstream/main``
    so the owner is pinged within a day instead of noticing weeks later.

    Silent (returns []) when the repo can't be located, when the checkout is a
    SHALLOW clone or otherwise has no shared history with ``upstream/main`` (the
    behind-count is meaningless there — see ``_upstream_lag_unmeasurable``), or
    when ``upstream/main`` is unavailable — best-effort, never a false alarm.
    """
    repo = repo_dir or _resolve_repo_dir()
    if repo is None:
        return []

    # Installer checkouts are shallow (`git clone --depth 1` in scripts/install.sh
    # / install.ps1). Across the shallow boundary HEAD shares no ancestry with
    # upstream/main, so `rev-list --count HEAD..upstream/main` counts ~ALL upstream
    # history (~13k) instead of the true distance — a phantom "fork is ~13000
    # commits behind" alarm fired DAILY on every onboarded client (upgrade.sh
    # registers this watchdog). Shallow is the INTENDED client default, and
    # upstream-lag is the fork maintainer's concern: the evolution server is a full
    # clone and still gets the real count. So skip silently here — mirrors the
    # shallow guards already in hermes_cli/banner.py and hermes_cli/main.py.
    if _upstream_lag_unmeasurable(runner, repo):
        return []

    try:
        rc, out = runner(
            ["git", "-C", str(repo), "rev-list", "--count", "HEAD..upstream/main"]
        )
    except Exception:  # noqa: BLE001 — any git/spawn failure: skip silently
        return []
    if rc != 0:
        return []
    try:
        behind = int(out.strip().split()[0])
    except (ValueError, IndexError):
        return []
    if behind > UPSTREAM_BEHIND_ALERT:
        return [
            f"upstream sync stuck: fork is {behind} commits behind upstream/main "
            f"(threshold {UPSTREAM_BEHIND_ALERT}). The daily sync escalates instead "
            f"of merging — resolve the backlog (see the open [UPSTREAM] issue)."
        ]
    return []


def _upstream_lag_unmeasurable(
    runner: Callable[[List[str]], Tuple[int, str]], repo: Path
) -> bool:
    """True when ``HEAD..upstream/main`` can't yield a meaningful behind-count.

    Two independent signals, either of which makes the numeric count a phantom:
      1. shallow repo — ``git rev-parse --is-shallow-repository`` == "true"
         (the `git clone --depth 1` installer default);
      2. no shared history — ``git merge-base HEAD upstream/main`` exits non-zero
         with EMPTY stdout (HEAD and upstream share no common ancestor, e.g. a
         grafted clone, even when the shallow flag is unset).

    Best-effort and FAIL-OPEN: any spawn error or inconclusive result returns
    False, so a normal full clone proceeds to the real rev-list count exactly as
    before — this can never make the check worse than today.
    """
    try:
        rc, out = runner(
            ["git", "-C", str(repo), "rev-parse", "--is-shallow-repository"]
        )
        if rc == 0 and out.strip() == "true":
            return True
    except Exception:  # noqa: BLE001 — inconclusive probe: don't block the real check
        return False

    try:
        rc, out = runner(["git", "-C", str(repo), "merge-base", "HEAD", "upstream/main"])
    except Exception:  # noqa: BLE001
        return False
    # No common ancestor: git exits non-zero with NOTHING on stdout. A non-zero
    # exit WITH output (e.g. "fatal: bad revision 'upstream/main'" when the remote
    # is merely missing) is the unrelated missing-remote case — leave that to the
    # rev-list step, which already fails silently, so we don't turn a missing
    # remote into a spurious shallow skip.
    if rc != 0 and not out.strip():
        return True
    return False


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


def check_realized_impact(evolution_dir: Path) -> List[str]:
    """Alert when the post-merge realized-impact sidecar reports blind evolution.

    evolution_realized_impact writes ``realized-impact.txt`` ending in
    ``| healthy`` when merged changes land real value, or ``| <FLAGS>``
    (REALIZED_IMPACT_LOW / REALIZED_RATE_LOW / UNVERIFIED_BACKLOG) when the agent
    is shipping plausible-but-useless code or the verification step has stopped
    running. This is the loop that stops the agent from optimizing a predicted
    impact it never checks against reality. Silent when healthy or absent."""
    try:
        line = (evolution_dir / "realized-impact.txt").read_text(encoding="utf-8").strip()
    except OSError:
        return []
    if not line or line.endswith("| healthy"):
        return []
    return [f"realized-impact degraded: {line}"]


def check_analysis_integrity(evolution_dir: Path) -> List[str]:
    """Alert when the latest analysis cycle's self-reported selection budget is
    illegal or overspent (PR #519's effort-budget contract — the agent once wrote
    max_total_effort=2.0, neither 1.5 nor 3.0), OR when an ``already-exists``
    rejection cited repo paths that do not exist (the #83 fabricated-close class —
    needs the repo, resolved via _resolve_repo_dir). Silent when clean, when there
    is no dated analysis report yet, or when the audit module is unavailable (the
    scheduler installs evolution_*.py alongside this script, so the sibling import
    resolves at runtime; the guard keeps unit imports safe)."""
    try:
        from evolution_analysis_audit import audit_latest
    except ImportError:
        return []
    return [
        f"analysis selection integrity: {v}"
        for v in audit_latest(evolution_dir, _resolve_repo_dir())
    ]


# ---------------------------------------------------------------------------
# Edge-triggering for the steady-state HEALTH alerts.
#
# WHY: the pipeline-health checks (check_health / check_realized_impact /
# check_analysis_integrity) re-emit the SAME alert on EVERY cron run while a
# known, already-throttled condition persists (e.g. selection_efficiency=11%,
# self-corrected by PR #519's deterministic effort_budget). Re-screaming a
# steady condition daily is pure fatigue — it adds no information.
#
# WHAT we do: alert on TRANSITIONS, not on steady state. We emit when
#   • a NEW flag/condition appears that wasn't present last run,
#   • a condition WORSENS (a new/harsher flag, or an embedded counter such as
#     `MERGED_ZERO x3 -> x5` grows — both change the flag tail = the signature),
#   • a condition CLEARS (recovery — announced once),
#   • a condition has persisted UNCHANGED for >= EDGE_COOLDOWN_DAYS days
#     (a single "still unresolved" nudge so it is never silently forgotten).
# We SUPPRESS only the verbatim repeat of an already-reported, non-worsening
# condition within the cooldown window.
#
# NO-MASK SAFETY PROPERTY: suppression keys on a *condition signature* (the
# sorted flag tails), so any new fault, any worsening, and any new distinct
# flag changes the signature and emits immediately. Suppression can ONLY hide a
# byte-for-byte-equivalent condition we already reported. Operational alerts
# (stage reports, jobs, gh, upstream-lag from #561) never pass through here.
#
# FAIL-OPEN CONTRACT: every state read/write is best-effort. A missing,
# unreadable, or corrupt state file means "unknown previous state" → we emit
# exactly as the watchdog does today. A write failure is swallowed (never
# crashes the run, never suppresses the current alert). Edge-triggering can
# therefore only ever REDUCE noise, never mask a fault.
#
# KNOWN BOUND (acceptable by design): the signature is the set of flag tails,
# so a *worsening WITHIN a single binary flag* (e.g. selection_efficiency
# 11% → 1%, both below the one LOW_SELECTION_EFFICIENCY threshold the sidecars
# expose) does not change the signature and is suppressed until either a new
# flag joins or the EDGE_COOLDOWN_DAYS re-reminder fires. The sidecars have no
# WARN/CRITICAL sub-tiers to cross, so there is no finer "worse threshold" to
# key on today; if one is added, extend the tail to include it. The cooldown
# nudge is the backstop that guarantees no condition is silent forever.
# ---------------------------------------------------------------------------


def health_signature(health_alerts: List[str]) -> str:
    """Stable, count-aware condition key for a set of health alerts.

    Keys on the FLAG TAIL of each alert (the text after the final ``|``), not
    the full descriptive line: the metrics body carries run-to-run counts
    (``cycles_active``, ``selected=…``) that drift even when the condition is
    unchanged — including the body would make every run look "new" and nothing
    would ever be suppressed. Embedded severity counters that live in the tail
    (``MERGED_ZERO x5``) DO change the signature, so a worsening still trips it.

    Order-independent (alerts are sorted) and returns ``""`` for no condition
    (healthy), which is the recovery sentinel.
    """
    tails: List[str] = []
    for alert in health_alerts:
        # The flag tail is everything after the last "| " separator that the
        # sidecars use to terminate the metrics body. When there is no such
        # separator (e.g. analysis-integrity alerts), the whole string IS the
        # condition.
        tail = alert.rsplit("| ", 1)[-1].strip() if "| " in alert else alert.strip()
        tails.append(tail)
    return "\n".join(sorted(tails))


def load_alert_state(state_path: Path) -> dict | None:
    """Read the persisted alert state. FAIL-OPEN: any miss/IO/parse error or a
    structurally invalid payload returns None (== unknown previous state)."""
    try:
        data = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or "signature" not in data:
        return None
    return data


def save_alert_state(state_path: Path, signature: str, last_emitted_at: datetime) -> None:
    """Persist the current signature + last-emitted timestamp. FAIL-OPEN: a
    write failure is swallowed — it must never crash the run nor (by raising)
    suppress an alert the caller already decided to emit."""
    try:
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(
            json.dumps(
                {"signature": signature, "last_emitted_at": last_emitted_at.isoformat()}
            ),
            encoding="utf-8",
        )
    except OSError:
        return


def apply_edge_trigger(
    health_alerts: List[str],
    state_path: Path,
    now: datetime,
    cooldown_days: int = EDGE_COOLDOWN_DAYS,
) -> List[str]:
    """Decide which HEALTH alerts to actually emit this run, and persist state.

    Returns the alerts to print (possibly a single recovery/reminder line in
    place of the raw alerts). See the design block above for the full rules.
    """
    sig = health_signature(health_alerts)
    prev = load_alert_state(state_path)
    prev_sig = prev.get("signature") if prev else None
    prev_ts = _parse_iso(prev.get("last_emitted_at")) if prev else None

    # --- Transition: the condition changed (or we have no prior state) -------
    if sig != prev_sig:
        if sig == "":
            # Cleared. Announce recovery exactly once IF we actually had a prior
            # non-empty condition on record. (prev_sig is None on a fresh/corrupt
            # state with nothing wrong → nothing to recover, stay silent.)
            if prev_sig:
                save_alert_state(state_path, "", now)
                return [
                    "pipeline health RECOVERED: previously-flagged condition has "
                    "cleared (no health flags this run)"
                ]
            # Fail-open with no condition: record the healthy baseline, emit nothing.
            save_alert_state(state_path, "", now)
            return []
        # New / worsening / changed condition (or fail-open unknown prior) → emit.
        save_alert_state(state_path, sig, now)
        return health_alerts

    # --- Steady state: signature identical to what we last saw ---------------
    if sig == "":
        # Still healthy — nothing to say, keep the baseline fresh.
        save_alert_state(state_path, "", now)
        return []

    # Identical non-empty condition. Suppress unless the cooldown elapsed.
    if prev_ts is not None and now - prev_ts >= timedelta(days=cooldown_days):
        # Long-cooldown re-reminder: never let a real fault go silent forever.
        save_alert_state(state_path, sig, now)  # reset the clock
        days = (now - prev_ts).days
        return [
            f"still unresolved after {days}d (no change since last alert) — {a}"
            for a in health_alerts
        ]
    # Verbatim repeat within cooldown → suppress. Do NOT refresh the timestamp,
    # so the re-reminder fires relative to the LAST real emission.
    return []


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

    # Operational alerts: acute infra/scheduler/sync failures. These are ALWAYS
    # emitted every run — they are not steady-state pipeline-health conditions
    # and must never be edge-suppressed (a broken gh or a stuck upstream-sync is
    # actionable every single day until fixed). The #561 upstream-lag guard is
    # untouched: its own shallow/no-shared-history checks decide if it speaks.
    operational: List[str] = []
    operational += check_stage_reports(evolution_dir, now, jobs_file)
    operational += check_jobs(jobs_file, now)
    operational += check_gh()
    operational += check_upstream_lag()

    # Pipeline-HEALTH alerts: steady-state calibration/quality conditions that
    # self-correct over time (effort_budget throttle) and re-fire identically
    # every run. Only THESE pass through the edge-trigger (transitions, not
    # steady state) — see the design block above. Fail-open: on any state error
    # the layer emits exactly as the watchdog does today.
    health: List[str] = []
    health += check_health(evolution_dir)
    health += check_realized_impact(evolution_dir)
    health += check_analysis_integrity(evolution_dir)
    health = apply_edge_trigger(health, evolution_dir / ALERT_STATE_FILENAME, now)

    alerts: List[str] = operational + health

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
