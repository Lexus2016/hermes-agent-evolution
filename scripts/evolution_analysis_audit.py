#!/usr/bin/env python3
"""Deterministic audit of an evolution-analysis cycle's selection output.

The analysis stage is prompt-driven: PR #507/#519 tell it to set this cycle's
``max_total_effort`` to the budget the metric script prescribes — 3.0 by default,
1.5 when ``LOW_SELECTION_EFFICIENCY`` is flagged — and to spend no more than that.
A prompt instruction is NOT enforced: the 2026-06-24 cycle wrote
``max_total_effort = 2.0`` (neither legal value) and under-throttled. This module
mechanically catches that class — the budget the agent self-reports must be one
of the two legal values, and the effort it actually selected must not exceed it.

Read+flag only (the watchdog surfaces it). A bad selection is not catastrophic
(the analysis stage merges nothing; the next cycle self-corrects), so a morning
alert to the owner is the right enforcement teeth for THIS stage — the same
deterministic-verdict pattern as evolution_skill_lint (#190) and the
realized-impact / regression gates.

Pure functions + explicit IO so it is import-safe and unit-testable.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

# The two budgets the metric script is allowed to prescribe (#519 contract).
# Anything else means the agent invented a number instead of copying one.
LEGAL_BUDGETS: Tuple[float, ...] = (1.5, 3.0)
_EPS = 1e-9


def _num(x: Any) -> Optional[float]:
    """Coerce to float, but reject bool (True/False are ints in Python)."""
    if isinstance(x, bool):
        return None
    if isinstance(x, (int, float)):
        return float(x)
    return None


def _selection_constraints(report: Dict[str, Any]) -> Dict[str, Any]:
    """``max_total_effort`` lives under ``scoring_model.selection_constraints``
    (observed shape), but tolerate a top-level ``selection_constraints`` too so a
    future report layout does not silently skip the check."""
    for container in (report.get("scoring_model"), report):
        if isinstance(container, dict):
            sc = container.get("selection_constraints")
            if isinstance(sc, dict) and "max_total_effort" in sc:
                return sc
    return {}


def audit_analysis(
    report: Dict[str, Any], legal_budgets: Sequence[float] = LEGAL_BUDGETS
) -> List[str]:
    """Return human-readable violation strings (empty == clean).

    Missing or non-numeric fields are SKIPPED, never flagged — a partial,
    legacy, or idle report must not raise a false alarm. Only a concrete,
    clearly-wrong value is reported.
    """
    if not isinstance(report, dict):
        return []
    out: List[str] = []

    sc = _selection_constraints(report)
    budget = _num(sc.get("max_total_effort"))

    if budget is not None and not any(abs(budget - b) < _EPS for b in legal_budgets):
        legal = "/".join(f"{b:g}" for b in legal_budgets)
        out.append(
            f"BUDGET_ILLEGAL: max_total_effort={budget:g} is neither legal value "
            f"({legal}) — the analysis agent invented a budget instead of copying "
            f"the metric script's prescribed one (PR #519 contract)"
        )

    spent = _num(report.get("total_effort_selected"))
    if budget is not None and spent is not None and spent > budget + _EPS:
        out.append(
            f"BUDGET_OVERSPENT: total_effort_selected={spent:g} exceeds "
            f"max_total_effort={budget:g} — the over-selection the throttle exists "
            f"to prevent"
        )

    return out


def audit_latest(evolution_dir: Path) -> List[str]:
    """Audit the most recent dated analysis report under ``<dir>/analysis/``.

    Returns prefixed violation strings, or [] when there is no readable dated
    report. Only ``YYYY-MM-DD.json`` files are considered — the sibling
    ``issues_*.json`` / ``prs_*.json`` snapshots are skipped.
    """
    analysis_dir = evolution_dir / "analysis"
    try:
        files = list(analysis_dir.glob("*.json"))
    except OSError:
        return []
    dated = sorted(f for f in files if re.fullmatch(r"\d{4}-\d{2}-\d{2}\.json", f.name))
    if not dated:
        return []
    latest = dated[-1]
    try:
        report = json.loads(latest.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [f"({latest.stem}) {v}" for v in audit_analysis(report)]


def main(argv: List[str]) -> int:
    import os

    evolution_dir = Path(
        os.environ.get(
            "EVOLUTION_PROFILE_DIR",
            str(Path.home() / ".hermes" / "profiles" / "user1" / "evolution"),
        )
    )
    violations = audit_latest(evolution_dir)
    for v in violations:
        print(f"[analysis-audit] {v}")
    return 1 if violations else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
