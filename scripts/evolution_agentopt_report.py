#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""AgentOpt Slice 2 — per-combo aggregation + Pareto frontier (#2742).

Child of #2695. Deterministic aggregation over the Slice-1 telemetry JSONL
(``agentopt_telemetry.record_llm_call``): per model-combination cost /
latency / accuracy stats per task class, and the Pareto frontier of
non-dominated combinations — the measured baseline Slice 3 feeds into the
router.

Grouping (deterministic; absent fields degrade, never crash):
- ``combo``    — record["combo"] if present, else the single model name.
- ``task``     — record["task_class"] or record["tool"] or "default".

CLI: read the default Slice-1 store and print the aggregate as JSON.
Exit 0 always — an empty/missing store reports empty stats, not an error.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.agentopt_telemetry import _store_path  # noqa: E402

ComboKey = str  # "model-a+model-b" — sorted, deduplicated, "+"-joined


def combo_key(record: Dict[str, Any]) -> ComboKey:
    """Identity of the model combination that served one call."""
    raw = record.get("combo")
    models = (
        [str(m) for m in raw if str(m)]
        if isinstance(raw, (list, tuple))
        else ([str(raw)] if raw else [str(record.get("model") or "unknown")])
    )
    return "+".join(sorted(set(models)))


def task_class(record: Dict[str, Any]) -> str:
    """Task class of one call — combo stats are reported per class."""
    for field in ("task_class", "task", "tool"):
        value = record.get(field)
        if isinstance(value, str) and value:
            return value
    return "default"


def load_records(store: Path) -> List[Dict[str, Any]]:
    """Parse the Slice-1 JSONL; malformed lines are skipped, never fatal."""
    try:
        lines = store.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out: List[Dict[str, Any]] = []
    for line in lines:
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if isinstance(rec, dict):
            out.append(rec)
    return out


def aggregate(records: Iterable[Dict[str, Any]]) -> Dict[str, Dict[ComboKey, Dict[str, Any]]]:
    """Per task class × combo: calls, cost, mean latency, success rate."""
    acc: Dict[str, Dict[ComboKey, Dict[str, Any]]] = {}
    for rec in records:
        cls, combo = task_class(rec), combo_key(rec)
        bucket = acc.setdefault(cls, {}).setdefault(
            combo, {"calls": 0, "cost": 0.0, "latency_sum": 0.0, "ok": 0}
        )
        bucket["calls"] += 1
        try:
            bucket["cost"] += float(rec.get("cost") or 0.0)
        except (TypeError, ValueError):
            pass
        try:
            bucket["latency_sum"] += float(rec.get("latency_ms") or 0.0)
        except (TypeError, ValueError):
            pass
        if str(rec.get("outcome") or "").lower() in ("ok", "success", "true"):
            bucket["ok"] += 1
    for cls in acc:
        for combo, bucket in acc[cls].items():
            calls = bucket["calls"]
            acc[cls][combo] = {
                "calls": calls,
                "cost": round(bucket["cost"], 6),
                "avg_latency_ms": round(bucket["latency_sum"] / calls, 3) if calls else 0.0,
                "success_rate": round(bucket["ok"] / calls, 4) if calls else 0.0,
            }
    return acc


def _dominates(a: Dict[str, Any], b: Dict[str, Any]) -> bool:
    """a dominates b: no worse on cost/latency/success, strictly better somewhere."""
    return (
        a["cost"] <= b["cost"]
        and a["avg_latency_ms"] <= b["avg_latency_ms"]
        and a["success_rate"] >= b["success_rate"]
        and (
            a["cost"] < b["cost"]
            or a["avg_latency_ms"] < b["avg_latency_ms"]
            or a["success_rate"] > b["success_rate"]
        )
    )


def pareto_frontier(stats: Dict[ComboKey, Dict[str, Any]]) -> List[ComboKey]:
    """Combos not dominated by any other — deterministic (sorted) order."""
    frontier = [
        combo
        for combo, own in stats.items()
        if not any(
            other != combo and _dominates(stats[other], own) for other in stats
        )
    ]
    return sorted(frontier)


def build_report(records: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    """Aggregate + frontier per task class — the Slice-3 router baseline."""
    agg = aggregate(records)
    return {
        "task_classes": {
            cls: {"combos": stats, "pareto_frontier": pareto_frontier(stats)}
            for cls, stats in agg.items()
        }
    }


def main(argv: List[str]) -> int:
    path = Path(argv[1]) if len(argv) > 1 else _store_path()
    report = build_report(load_records(path))
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
