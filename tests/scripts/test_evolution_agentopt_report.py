# -*- coding: utf-8 -*-
"""AgentOpt Slice 2 — aggregation + Pareto frontier tests (#2742)."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from evolution_agentopt_report import (  # noqa: E402
    aggregate,
    build_report,
    combo_key,
    load_records,
    pareto_frontier,
    task_class,
)


def _rec(model, outcome="ok", cost=0.01, latency=100.0, **extra):
    return {"model": model, "outcome": outcome, "cost": cost,
            "latency_ms": latency, **extra}


def test_combo_key_models_and_field_variants():
    assert combo_key({"model": "m1"}) == "m1"
    assert combo_key({"combo": ["m2", "m1"], "model": "x"}) == "m1+m2"  # sorted
    assert combo_key({"combo": "solo"}) == "solo"
    assert combo_key({}) == "unknown"


def test_task_class_fallback_chain():
    assert task_class({"task_class": "coding"}) == "coding"
    assert task_class({"task": "t1", "tool": "terminal"}) == "t1"
    assert task_class({"tool": "terminal"}) == "terminal"
    assert task_class({}) == "default"


def test_aggregate_stats_math():
    stats = aggregate([
        _rec("m1", cost=0.02, latency=100.0, task_class="coding"),
        _rec("m1", outcome="fail", cost=0.04, latency=300.0, task_class="coding"),
        _rec("m2", cost=0.01, latency=50.0, task_class="coding"),
    ])
    coding = stats["coding"]
    assert coding["m1"]["calls"] == 2
    assert coding["m1"]["cost"] == 0.06
    assert coding["m1"]["avg_latency_ms"] == 200.0
    assert coding["m1"]["success_rate"] == 0.5
    assert coding["m2"]["success_rate"] == 1.0


def test_pareto_frontier_dominated_and_nondominated():
    stats = {
        "cheap-fast-good": {"cost": 0.01, "avg_latency_ms": 50.0, "success_rate": 0.99, "calls": 5},
        "expensive-slow-bad": {"cost": 0.05, "avg_latency_ms": 200.0, "success_rate": 0.90, "calls": 5},
        "cheap-but-inaccurate": {"cost": 0.005, "avg_latency_ms": 60.0, "success_rate": 0.70, "calls": 5},
    }
    frontier = pareto_frontier(stats)
    # The all-round worse combo is dominated off; the two trade-off combos stay.
    assert "cheap-fast-good" in frontier and "cheap-but-inaccurate" in frontier
    assert "expensive-slow-bad" not in frontier


def test_load_records_skips_malformed_lines(tmp_path):
    store = tmp_path / "calls.jsonl"
    store.write_text(
        json.dumps(_rec("m1")) + "\nnot json\n" + json.dumps(_rec("m2")) + "\n",
        encoding="utf-8",
    )
    assert [r["model"] for r in load_records(store)] == ["m1", "m2"]
    assert load_records(tmp_path / "missing.jsonl") == []


def test_build_report_end_to_end(tmp_path):
    store = tmp_path / "calls.jsonl"
    store.write_text(
        json.dumps(_rec("m1", cost=0.1, latency=500.0, task_class="eval"))
        + "\n"
        + json.dumps(_rec("m2", cost=0.01, latency=50.0, task_class="eval"))
        + "\n",
        encoding="utf-8",
    )
    report = build_report(load_records(store))
    eval_cls = report["task_classes"]["eval"]
    assert eval_cls["pareto_frontier"] == ["m2"]  # dominates m1 outright
    assert eval_cls["combos"]["m2"]["success_rate"] == 1.0
