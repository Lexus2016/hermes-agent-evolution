#!/usr/bin/env python3
"""Longitudinal meta-evolution metrics — is the pipeline actually getting better?

The funnel records ONE per-cycle line; this reads the whole history and answers
the meta question (issue #84 / recommendations rec 4.3): across cycles, is the
pipeline healthy and improving, or stagnating? You cannot honestly trust a system
for autonomy you cannot measure — this is the measurement spine that makes
"is it good enough yet?" an evidence question instead of a vibe.

Metrics (all deterministic, from the existing metrics.jsonl — no creds, no LLM):
  * cycle_success_rate — of ACTIVE cycles (something was selected/created), the
    fraction that landed >=1 merge. Idle days are not counted as failures.
  * selection_efficiency — total merged / total selected over the window. A
    CALIBRATION proxy: of what the pipeline DECIDED to implement, how much
    actually merged. Low = it picks work it can't land (poor self-capability
    calibration). Proxy only — selection and its merge can fall in different
    cycles (the pipeline is async), so read it over a window, not per-cycle.
  * reject_rate — rejected / (selected + rejected): triage selectivity.
  * merged_trend — mean merges in the window's 2nd half vs 1st half: improving /
    flat / declining.

Pure functions + explicit IO so it is import-safe and unit-testable. Reuses
evolution_funnel.load_records (single source of truth for reading the log).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
from evolution_funnel import is_evolution_halted, load_records  # noqa: E402


def _int(rec: Dict[str, Any], key: str) -> int:
    try:
        return int(rec.get(key, 0) or 0)
    except (TypeError, ValueError):
        return 0


def _is_active(rec: Dict[str, Any]) -> bool:
    """A cycle that actually had work — not an idle day or a premature/artifact
    all-zero record. Idle days must not drag down success rate."""
    return _int(rec, "selected") > 0 or _int(rec, "issues_created") > 0


def _trend(values: List[int]) -> str:
    """improving / flat / declining by comparing 2nd-half mean to 1st-half mean."""
    n = len(values)
    if n < 2:
        return "n/a"
    mid = n // 2
    first = values[:mid]
    second = values[mid:]
    if not first or not second:
        return "n/a"
    a = sum(first) / len(first)
    b = sum(second) / len(second)
    if b > a * 1.15:
        return "improving"
    if b < a * 0.85:
        return "declining"
    return "flat"


def compute_health(
    records: List[Dict[str, Any]],
    last: int = 30,
    evolution_dir: Path | None = None,
) -> Dict[str, Any]:
    window = records[-last:] if last and last > 0 else list(records)
    active = [r for r in window if _is_active(r)]

    if evolution_dir is None:
        evolution_dir = Path(
            os.environ.get(
                "EVOLUTION_PROFILE_DIR",
                str(Path.home() / ".hermes" / "profiles" / "user1" / "evolution"),
            )
        )

    sel = sum(_int(r, "selected") for r in active)
    mrg = sum(_int(r, "merged") for r in active)
    rej = sum(_int(r, "rejected") for r in active)
    created = sum(_int(r, "issues_created") for r in active)
    triaged = sel + rej

    succeeded = sum(1 for r in active if _int(r, "merged") > 0)
    cycle_success_rate = (succeeded / len(active)) if active else None
    selection_efficiency = (mrg / sel) if sel else None
    reject_rate = (rej / triaged) if triaged else None

    flags: List[str] = []
    # Only judge once there is enough signal to mean anything.
    if len(active) >= 3:
        if cycle_success_rate is not None and cycle_success_rate < 0.34:
            flags.append("LOW_SUCCESS: <1/3 of active cycles land a merge")
        if selection_efficiency is not None and selection_efficiency < 0.34:
            flags.append(
                "LOW_SELECTION_EFFICIENCY: picks more than it can land "
                "(poor self-capability calibration)"
            )

    # Deterministic effort budget for the NEXT selection cycle. The analysis
    # stage copies this verbatim instead of deriving "1.5 vs 3.0" from the flag
    # itself — a prompt-level decision that drifted to arbitrary middles like 2.0
    # (observed 2026-06-24, under-throttling while the watchdog kept firing). The
    # ONLY two legal values are the throttled budget and the default; the script
    # owns the choice, the agent owns nothing but the copy.
    low_selection = any(f.startswith("LOW_SELECTION_EFFICIENCY") for f in flags)
    effort_budget = 1.5 if low_selection else 3.0

    # Halt-state visibility (#770): the hydra gate and cron scheduler check
    # halt-state.txt to suppress expensive LLM stages. Surfacing it in the
    # health sidecar makes the automated halt obvious to the owner without
    # needing to inspect a separate file.
    halted = is_evolution_halted(evolution_dir)
    if halted:
        flags.append(
            "HALTED: pipeline suspended — zero deliverables; clear halt-state.txt to resume"
        )

    return {
        "cycles_total": len(window),
        "cycles_active": len(active),
        "issues_created": created,
        "selected": sel,
        "merged": mrg,
        "rejected": rej,
        "cycle_success_rate": round(cycle_success_rate, 3)
        if cycle_success_rate is not None
        else None,
        "selection_efficiency": round(selection_efficiency, 3)
        if selection_efficiency is not None
        else None,
        "reject_rate": round(reject_rate, 3) if reject_rate is not None else None,
        "merged_trend": _trend([_int(r, "merged") for r in active]),
        "effort_budget": effort_budget,
        "halted": halted,
        "flags": flags,
    }


def _pct(x: Optional[float]) -> str:
    return "n/a" if x is None else f"{x:.0%}"


def format_health(h: Dict[str, Any]) -> str:
    tail = " | ".join(h["flags"]) if h["flags"] else "healthy"
    # NOTE: effort_budget rides in the BODY, never the tail. evolution_watchdog
    # keys on `.endswith("| healthy")` / `| <FLAG>`, so the flags must stay the
    # last segment after the final `|`.
    return (
        f"[evolution-metrics] {h['cycles_active']}/{h['cycles_total']} active cycles: "
        f"success={_pct(h['cycle_success_rate'])} "
        f"selection_efficiency={_pct(h['selection_efficiency'])} "
        f"reject_rate={_pct(h['reject_rate'])} merged_trend={h['merged_trend']} "
        f"(created={h['issues_created']} selected={h['selected']} merged={h['merged']}) "
        f"effort_budget={h['effort_budget']:.1f} | {tail}"
    )


def main(argv: List[str]) -> int:
    import os

    evolution_dir = Path(
        os.environ.get(
            "EVOLUTION_PROFILE_DIR",
            str(Path.home() / ".hermes" / "profiles" / "user1" / "evolution"),
        )
    )
    args = argv[1:]
    last = 30
    if "--last" in args:
        i = args.index("--last")
        if i + 1 < len(args):
            try:
                last = int(args[i + 1])
            except ValueError:
                last = 30
    records = load_records(evolution_dir / "metrics.jsonl")
    print(format_health(compute_health(records, last, evolution_dir=evolution_dir)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
