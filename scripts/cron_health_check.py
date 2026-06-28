#!/usr/bin/env python3
"""Cron health monitor — issue #608.

Aggregates recent cron failure records and produces a structured health report
so that a 100% cron-session failure rate (e.g. DeepReadError/APIConnectionError
from a pinned provider) becomes visible instead of failing silently.

The script is intentionally read-only by default; it never mutates cron jobs or
schedules. With ``--create-issue`` it opens a single GitHub issue when the
cron fleet is unhealthy, so operators are alerted without flooding the issue
tracker (one open alert at a time).

Usage
-----
    python scripts/cron_health_check.py [--days N] [--alert-threshold F]
        [--consecutive-threshold N] [--create-issue] [--dry-run]

Exit codes
----------
    0  healthy (or no recent data)
    1  unhealthy cron jobs detected
    2  runtime error
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from hermes_constants import get_hermes_home

logger = logging.getLogger(__name__)

_DEFAULT_REPO = "Lexus2016/hermes-agent-evolution"
_DEFAULT_DAYS = 7
_DEFAULT_ALERT_THRESHOLD = 0.75
_DEFAULT_CONSECUTIVE_THRESHOLD = 3
_DEFAULT_MIN_SAMPLES = 2


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_iso(ts: Any) -> Optional[datetime]:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(str(ts))
    except (ValueError, TypeError):
        return None


def _health_dir() -> Path:
    return get_hermes_home() / "cron" / "health"


def _load_jobs() -> List[Dict[str, Any]]:
    from cron.jobs import list_jobs

    return list_jobs(include_disabled=True)


def _load_failures(
    job_id: str, days: int, max_records: int = 50
) -> List[Dict[str, Any]]:
    """Read recent failure records for a job from the current HERMES_HOME."""
    from hermes_constants import get_hermes_home

    failure_job_dir = get_hermes_home() / "cron" / "failures" / job_id
    if not failure_job_dir.exists():
        return []
    cutoff = _now() - timedelta(days=days)
    recent = []
    paths = list(failure_job_dir.glob("*.json"))

    # Sort by the timestamp in the record (chronological, newest first).
    def _sort_key(path: Path) -> float:
        try:
            ts = _parse_iso(
                json.loads(path.read_text(encoding="utf-8")).get("timestamp")
            )
        except Exception:
            ts = None
        if ts is None:
            return -1.0
        return ts.timestamp()

    for path in sorted(paths, key=_sort_key, reverse=True):
        try:
            rec = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        ts = _parse_iso(rec.get("timestamp"))
        if ts is None:
            continue
        if ts >= cutoff:
            recent.append(rec)
        if len(recent) >= max_records:
            break
    return recent


def _consecutive_failures(records: Sequence[Dict[str, Any]]) -> int:
    count = 0
    for rec in records:
        if not rec.get("success", True):
            count += 1
        else:
            break
    return count


def _job_health(
    job: Dict[str, Any],
    records: Sequence[Dict[str, Any]],
    alert_threshold: float,
    consecutive_threshold: int,
    min_samples: int,
) -> Dict[str, Any]:
    total = len(records)
    failures = [r for r in records if not r.get("success", True)]
    failure_count = len(failures)
    failure_rate = failure_count / total if total else 0.0
    categories = Counter(str(r.get("failure_category") or "unknown") for r in failures)
    consecutive = _consecutive_failures(records)

    unhealthy = total >= min_samples and (
        failure_rate >= alert_threshold or consecutive >= consecutive_threshold
    )

    return {
        "job_id": job.get("id"),
        "job_name": job.get("name") or job.get("id"),
        "model": job.get("model"),
        "provider": job.get("provider"),
        "total_runs": total,
        "failure_count": failure_count,
        "failure_rate": round(failure_rate, 3),
        "consecutive_failures": consecutive,
        "categories": dict(categories.most_common()),
        "unhealthy": bool(unhealthy),
    }


def _aggregate_provider_model(
    job_healths: Sequence[Dict[str, Any]],
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    providers: Dict[str, Dict[str, Any]] = {}
    models: Dict[str, Dict[str, Any]] = {}

    for h in job_healths:
        for bucket, key in ((providers, h.get("provider")), (models, h.get("model"))):
            if not key:
                continue
            entry = bucket.setdefault(
                key, {"total_runs": 0, "failure_count": 0, "categories": Counter()}
            )
            entry["total_runs"] += h["total_runs"]
            entry["failure_count"] += h["failure_count"]
            for cat, cnt in (h.get("categories") or {}).items():
                entry["categories"][cat] += cnt

    def _finalize(bucket: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
        result: Dict[str, Any] = {}
        for key, entry in bucket.items():
            total = entry["total_runs"]
            result[key] = {
                "total_runs": total,
                "failure_count": entry["failure_count"],
                "failure_rate": round(entry["failure_count"] / total, 3)
                if total
                else 0.0,
                "categories": dict(entry["categories"].most_common()),
            }
        return result

    return _finalize(providers), _finalize(models)


def build_report(
    days: int = _DEFAULT_DAYS,
    alert_threshold: float = _DEFAULT_ALERT_THRESHOLD,
    consecutive_threshold: int = _DEFAULT_CONSECUTIVE_THRESHOLD,
    min_samples: int = _DEFAULT_MIN_SAMPLES,
) -> Dict[str, Any]:
    jobs = _load_jobs()
    job_healths: List[Dict[str, Any]] = []

    for job in jobs:
        records = _load_failures(str(job.get("id") or ""), days)
        if not records:
            continue
        job_healths.append(
            _job_health(
                job, records, alert_threshold, consecutive_threshold, min_samples
            )
        )

    providers, models = _aggregate_provider_model(job_healths)
    unhealthy_jobs = [h for h in job_healths if h["unhealthy"]]
    healthy = not bool(unhealthy_jobs)

    return {
        "generated_at": _now().isoformat(),
        "lookback_days": days,
        "alert_threshold": alert_threshold,
        "consecutive_threshold": consecutive_threshold,
        "min_samples": min_samples,
        "healthy": healthy,
        "total_jobs_with_data": len(job_healths),
        "unhealthy_jobs": unhealthy_jobs,
        "jobs": job_healths,
        "provider_summary": providers,
        "model_summary": models,
    }


def _save_report(report: Dict[str, Any]) -> Path:
    out_dir = _health_dir()
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / "latest.json"
    tmp = out_dir / f".latest.json.{os.getpid()}.tmp"
    try:
        tmp.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
        tmp.replace(out_file)
    finally:
        if tmp.exists():
            tmp.unlink()
    return out_file


def _format_alert_body(report: Dict[str, Any]) -> str:
    lines = [
        "Cron fleet health check detected unhealthy jobs.",
        "",
        f"* Generated at: {report['generated_at']}",
        f"* Lookback: {report['lookback_days']} days",
        f"* Alert threshold: {report['alert_threshold'] * 100:.0f}% failure rate",
        f"* Consecutive threshold: {report['consecutive_threshold']} failures",
        "",
        "### Unhealthy jobs",
        "",
    ]
    for h in report["unhealthy_jobs"]:
        lines.append(
            f"- **{h['job_name']}** (`{h['job_id']}`): "
            f"{h['failure_count']}/{h['total_runs']} failures "
            f"({h['failure_rate'] * 100:.0f}%), "
            f"consecutive={h['consecutive_failures']}, "
            f"model={h['model']!r}, provider={h['provider']!r}"
        )
        if h.get("categories"):
            lines.append("  - failure categories: " + ", ".join(h["categories"].keys()))
    lines.extend(["", f"Full report: `{_health_dir() / 'latest.json'}`"])
    return "\n".join(lines)


def _find_open_alert(repo: str) -> Optional[str]:
    try:
        result = subprocess.run(
            [
                "gh",
                "issue",
                "list",
                "--repo",
                repo,
                "--state",
                "open",
                "--label",
                "cron-health",
                "--json",
                "number,title",
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if result.returncode != 0:
            return None
        data = json.loads(result.stdout)
        for item in data:
            title = item.get("title") or ""
            if title.startswith("Cron health alert:"):
                return str(item.get("number"))
        return None
    except Exception:
        return None


def _create_alert_issue(
    repo: str,
    report: Dict[str, Any],
    dry_run: bool,
) -> Optional[str]:
    title = (
        f"Cron health alert: {len(report['unhealthy_jobs'])} job(s) "
        f"unhealthy as of {_now().strftime('%Y-%m-%d %H:%M UTC')}"
    )
    body = _format_alert_body(report)
    if dry_run:
        print(f"[DRY-RUN] Would create issue:\n  title: {title}\n  body:\n{body}")
        return None
    existing = _find_open_alert(repo)
    if existing:
        logger.info(
            "Open cron-health alert already exists (#%s); skipping new issue.", existing
        )
        return existing
    try:
        result = subprocess.run(
            [
                "gh",
                "issue",
                "create",
                "--repo",
                repo,
                "--title",
                title,
                "--body",
                body,
                "--label",
                "cron-health",
            ],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        if result.returncode != 0:
            logger.error("Failed to create alert issue: %s", result.stderr.strip())
            return None
        url = result.stdout.strip().splitlines()[-1]
        logger.info("Created cron-health alert issue: %s", url)
        return url
    except Exception as e:
        logger.error("Failed to create alert issue: %s", e)
        return None


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Cron fleet health monitor")
    parser.add_argument(
        "--days",
        type=int,
        default=_DEFAULT_DAYS,
        help=f"Lookback window in days (default {_DEFAULT_DAYS})",
    )
    parser.add_argument(
        "--alert-threshold",
        type=float,
        default=_DEFAULT_ALERT_THRESHOLD,
        help=f"Failure-rate threshold to flag a job (default {_DEFAULT_ALERT_THRESHOLD})",
    )
    parser.add_argument(
        "--consecutive-threshold",
        type=int,
        default=_DEFAULT_CONSECUTIVE_THRESHOLD,
        help=f"Consecutive-failure threshold to flag a job (default {_DEFAULT_CONSECUTIVE_THRESHOLD})",
    )
    parser.add_argument(
        "--create-issue",
        action="store_true",
        help="Create a GitHub issue when unhealthy jobs are detected",
    )
    parser.add_argument(
        "--repo",
        default=_DEFAULT_REPO,
        help=f"Target repo for issue creation (default {_DEFAULT_REPO})",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the report and would-be issue but do not write or create anything",
    )
    parser.add_argument(
        "--min-samples",
        type=int,
        default=_DEFAULT_MIN_SAMPLES,
        help=f"Minimum runs required before alerting on rate (default {_DEFAULT_MIN_SAMPLES})",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    try:
        report = build_report(
            days=args.days,
            alert_threshold=args.alert_threshold,
            consecutive_threshold=args.consecutive_threshold,
            min_samples=args.min_samples,
        )
    except Exception as e:
        logger.error("Failed to build health report: %s", e)
        return 2

    if args.dry_run:
        print(json.dumps(report, indent=2, default=str))
    else:
        try:
            out_file = _save_report(report)
            logger.info("Health report saved to %s", out_file)
        except Exception as e:
            logger.error("Failed to save report: %s", e)
            return 2

    if report["unhealthy_jobs"]:
        logger.warning(
            "Unhealthy cron jobs detected: %s",
            ", ".join(h["job_name"] for h in report["unhealthy_jobs"]),
        )
        if args.create_issue:
            _create_alert_issue(args.repo, report, dry_run=args.dry_run)
        return 1

    logger.info(
        "Cron fleet healthy (%d jobs with recent data).", report["total_jobs_with_data"]
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
