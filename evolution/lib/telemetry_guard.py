"""Integrity checks on telemetry that steers evolution (#2637).

The evolution loop reads sidecar files (``metrics.jsonl``, ``evolution-health.txt``)
as trusted inputs that steer selection; tampered telemetry can steer the loop.
This module validates those inputs and FAILS CLOSED: malformed or tampered
content is reported unsafe, never silently accepted. Pure + deterministic.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict

_REQUIRED = ("date", "issues_created", "selected", "rejected", "merged", "skipped")
_HEALTH_RE = re.compile(r"^\[evolution-metrics\]")
_EFFORT_RE = re.compile(r"effort_budget=\d+(?:\.\d+)?")


@dataclass(frozen=True)
class TelemetryVerdict:
    """Fail-closed verdict for one telemetry input."""

    safe: bool
    reason: str = ""


def _finite(value: Any) -> bool:
    # Real-number check; rejects bool and NaN/Inf (tampering/overflow markers).
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


def validate_metrics_record(record: Any) -> TelemetryVerdict:
    """Validate one funnel record (one line of metrics.jsonl)."""
    if not isinstance(record, dict):
        return TelemetryVerdict(False, "record is not a JSON object")
    missing = [k for k in _REQUIRED if k not in record]
    if missing:
        return TelemetryVerdict(False, f"missing required key(s): {', '.join(missing)}")
    if not isinstance(record["date"], str) or not record["date"]:
        return TelemetryVerdict(False, "date is not a non-empty string")
    bad = [k for k in _REQUIRED[1:] if not _finite(record[k]) or record[k] < 0]
    if bad:
        return TelemetryVerdict(
            False, f"count(s) not non-negative numbers: {', '.join(bad)}"
        )
    if any(isinstance(v, float) and not math.isfinite(v) for v in record.values()):
        return TelemetryVerdict(False, "record contains NaN/Infinity")
    return TelemetryVerdict(True)


def validate_metrics_line(line: str) -> TelemetryVerdict:
    """Validate one raw metrics.jsonl line; blank lines are skipped."""
    text = line.strip()
    if not text:
        return TelemetryVerdict(True, "blank line")
    try:
        return validate_metrics_record(json.loads(text))
    except ValueError as exc:
        return TelemetryVerdict(False, f"not valid JSON: {exc}")


def validate_health_text(text: str) -> TelemetryVerdict:
    """Validate evolution-health.txt; requires canonical prefix + budget token."""
    stripped = text.strip()
    if not _HEALTH_RE.match(stripped):
        return TelemetryVerdict(False, "health blob missing [evolution-metrics] prefix")
    if not _EFFORT_RE.search(stripped):
        return TelemetryVerdict(False, "health blob missing effort_budget=N token")
    return TelemetryVerdict(True)


def check_telemetry(evolution_dir: Path) -> Dict[str, Any]:
    """Validate steering telemetry; fail-closed aggregate (any unsafe -> unsafe)."""
    checks: Dict[str, Any] = {}
    metrics_file = evolution_dir / "metrics.jsonl"
    if metrics_file.exists():
        verdicts = [
            validate_metrics_line(line)
            for line in metrics_file.read_text(encoding="utf-8").splitlines()[-50:]
        ]
        first = next((v.reason for v in verdicts if not v.safe), None)
        checks["metrics"] = {
            "safe": first is None,
            "lines_checked": len(verdicts),
            "first_unsafe": first,
        }
    else:
        checks["metrics"] = {
            "safe": False,
            "lines_checked": 0,
            "first_unsafe": "metrics.jsonl missing",
        }
    health_file = evolution_dir / "evolution-health.txt"
    if health_file.exists():
        verdict = validate_health_text(health_file.read_text(encoding="utf-8"))
        checks["health"] = {"safe": verdict.safe, "reason": verdict.reason}
    else:
        checks["health"] = {"safe": False, "reason": "evolution-health.txt missing"}
    return {"safe": all(c["safe"] for c in checks.values()), "checks": checks}
