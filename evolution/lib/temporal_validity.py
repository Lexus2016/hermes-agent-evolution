# -*- coding: utf-8 -*-
"""Temporal-validity evaluation axis (issue #2842).

A positive evaluation criterion for any pipeline stage that reasons over dated
data: for a target date, verify each selected evidence/skill/memory item was
valid *as of* that date (not anachronistic or future-dated), and report a
temporal-validity pass rate alongside outcome metrics.

This is distinct from the 08-13 gist-compression temporal-preservation concern
(destroying timestamps in context); it is a *verification* criterion.  It
asserts against the same substrate that ``plugins/memory/mem0`` and
``tools/skill_usage.py`` write (``valid_from`` / ``valid_until`` windows), but
does not depend on those modules — it accepts any evidence carrying ISO-8601
``valid_from``/``valid_until`` (or ``timestamp``) fields, so it is a drop-in
assertion harness for eval/research stages.

Pure dataclasses, no external deps, import-safe, JSON round-trip, explicit
``encoding`` (ruff PLW1514).
"""

from __future__ import annotations

import calendar
import datetime
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

__all__ = [
    "EvidenceItem",
    "TemporalValidityReport",
    "evaluate_temporal_validity",
    "parse_iso_epoch",
]

_ISO_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$"
)


def parse_iso_epoch(value: Optional[str]) -> Optional[float]:
    """Parse an ISO-8601 timestamp to a UTC POSIX epoch, or None if malformed."""
    if not value or not isinstance(value, str):
        return None
    if not _ISO_RE.match(value):
        return None
    norm = value[:-1] + "+00:00" if value.endswith("Z") else value
    # Strip a trailing offset and parse the naive UTC wall-clock part.
    base = norm[:19]
    try:
        return calendar.timegm((
            int(base[0:4]),
            int(base[5:7]),
            int(base[8:10]),
            int(base[11:13]),
            int(base[14:16]),
            int(base[17:19]),
            0,
            0,
            0,
        ))
    except ValueError:
        return None


@dataclass
class EvidenceItem:
    """One dated evidence/result selected by a stage.

    ``valid_from`` / ``valid_until`` are ISO-8601 (or epoch float) bounds of
    when the evidence was true; ``timestamp`` is an optional shorthand for a
    single point-in-time item.  ``evidence`` links the audit trail.
    """

    id: str
    valid_from: Optional[str] = None
    valid_until: Optional[str] = None
    timestamp: Optional[str] = None
    evidence: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "EvidenceItem":
        return cls(
            id=str(data.get("id", "")),
            valid_from=data.get("valid_from"),
            valid_until=data.get("valid_until"),
            timestamp=data.get("timestamp"),
            evidence=list(data.get("evidence", [])),
        )


@dataclass
class TemporalValidityReport:
    """Aggregate temporal-validity result for one stage decision.

    ``pass_rate`` is the fraction of items valid as of ``as_of`` (0.0–1.0).
    ``violations`` lists the offending item ids with the reason.
    """

    as_of: str = ""
    total: int = 0
    passed: int = 0
    violations: List[Dict[str, str]] = field(default_factory=list)

    @property
    def pass_rate(self) -> float:
        return self.passed / self.total if self.total else 1.0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "TemporalValidityReport":
        return cls(
            as_of=str(data.get("as_of", "")),
            total=int(data.get("total", 0)),
            passed=int(data.get("passed", 0)),
            violations=list(data.get("violations", [])),
        )


def evaluate_temporal_validity(
    items: List[EvidenceItem],
    as_of: str,
) -> TemporalValidityReport:
    """Verify each item was valid *as of* ``as_of`` (ISO-8601).

    Rules:
    * Item with neither window nor timestamp → skipped (counted as passed,
      not a violation) — a piece of evidence with no date can't be anachronistic.
    * ``valid_from`` in the future relative to as_of → ``future-dated``.
    * ``valid_until`` (or ``timestamp``) before as_of → ``expired``.
    * ``timestamp`` after as_of → ``future-dated``.
    Returns a :class:`TemporalValidityReport`.
    """
    target = parse_iso_epoch(as_of)
    report = TemporalValidityReport(as_of=as_of)
    if target is None:
        return report  # malformed target: report empty (fail-open, no violations)
    for item in items:
        report.total += 1
        reason = _validity_reason(item, target)
        if reason is None:
            report.passed += 1
        else:
            report.violations.append({"id": item.id, "reason": reason})
    return report


def _validity_reason(item: EvidenceItem, target: float) -> Optional[str]:
    """Return a violation reason string, or None if the item is valid as-of."""
    from_ts = parse_iso_epoch(item.valid_from)
    until_ts = parse_iso_epoch(item.valid_until)
    point_ts = parse_iso_epoch(item.timestamp)
    if from_ts is None and until_ts is None and point_ts is None:
        return None  # undated → not anachronistic
    if from_ts is not None and target < from_ts:
        return "future-dated"
    if until_ts is not None and target >= until_ts:
        return "expired"
    if point_ts is not None and target < point_ts:
        return "future-dated"
    return None
