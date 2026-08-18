#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""AgentOpt Slice 3 — measured-combo router feed (#2743).

Child of #2695. Consumes the Slice-2 Pareto report
(``evolution_agentopt_report.build_report``) and feeds the measured
per-combo outcomes into the delegation routing table
(``tools.model_routing_table.RoutingTable.record_outcome``), replacing
static exploration-only signal with measured preference where it exists.

Deterministic, no LLM, no network; the table is persisted via its own
JSON round-trip so the next ``_route_subagent_model`` consultation
exploits the measured winners instead of cold-starting.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from evolution_agentopt_report import build_report, load_records  # noqa: E402


def feed_routing_table(
    report: Dict[str, Any],
    table: Any,
    *,
    min_calls: int = 5,
    only_frontier: bool = True,
) -> int:
    """Record measured outcomes into a RoutingTable; returns records added.

    Per task class (dimension): every frontier combo (or every combo when
    ``only_frontier=False``) with at least ``min_calls`` measured calls gets
    ONE success/failure outcome per call weighted by its success rate —
    using the table's own ``record_outcome`` API so persistence and
    epsilon-greedy semantics stay single-sourced. Models inside a multi-model
    combo each receive the combo-level signal (a combo succeeded together).
    """
    added = 0
    for cls, block in (report.get("task_classes") or {}).items():
        combos = block.get("combos") or {}
        frontier = set(block.get("pareto_frontier") or [])
        for combo, stats in combos.items():
            if only_frontier and combo not in frontier:
                continue
            calls = int(stats.get("calls") or 0)
            if calls < min_calls:
                continue
            successes = round(float(stats.get("success_rate") or 0.0) * calls)
            for model in combo.split("+"):
                for _ in range(successes):
                    table.record_outcome(model, cls, True)
                for _ in range(calls - successes):
                    table.record_outcome(model, cls, False)
                added += calls
    return added


def default_table_path() -> Path:
    """Where the delegation router already persists C-A-F tables (#2258)."""
    import os

    base = (
        os.environ.get("EVOLUTION_PROFILE_DIR", "").strip()
        or str(Path.home() / ".hermes" / "evolution")
    )
    return Path(base) / "agentopt-routing-table.json"


def run_feed(
    store: Optional[Path] = None, table_path: Optional[Path] = None
) -> int:
    """CLI entry: telemetry store -> Pareto report -> routing table file."""
    from tools.model_routing_table import RoutingTable

    table_path = table_path or default_table_path()
    try:
        table = RoutingTable.from_dict(json.loads(table_path.read_text(encoding="utf-8")))
    except (OSError, ValueError, KeyError, TypeError):
        table = RoutingTable(models=[])
    added = feed_routing_table(build_report(load_records(store or _store())), table)
    if added:
        table_path.parent.mkdir(parents=True, exist_ok=True)
        table_path.write_text(json.dumps(table.to_dict(), indent=2), encoding="utf-8")
    return added


def _store():
    from agent.agentopt_telemetry import _store_path

    return _store_path()


def main(argv: list) -> int:
    print(json.dumps({"outcomes_recorded": run_feed(Path(argv[1]) if len(argv) > 1 else None)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
