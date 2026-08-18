# -*- coding: utf-8 -*-
"""AgentOpt Slice 3 — measured-combo router feed tests (#2743)."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from evolution_agentopt_report import build_report  # noqa: E402
from evolution_agentopt_router_feed import (  # noqa: E402
    default_table_path,
    feed_routing_table,
    run_feed,
)
from tools.model_routing_table import RoutingTable  # noqa: E402


def _report():
    return build_report([
        # coding: m1 frontier (perfect, 6 calls), m2 dominated (fails)
        {"model": "m1", "outcome": "ok", "cost": 0.01, "latency_ms": 50.0, "task_class": "coding"},
        {"model": "m2", "outcome": "fail", "cost": 0.09, "latency_ms": 500.0, "task_class": "coding"},
    ] + [
        {"model": "m1", "outcome": "ok", "cost": 0.01, "latency_ms": 55.0, "task_class": "coding"}
    ] * 5 + [
        {"model": "m2", "outcome": "fail", "cost": 0.09, "latency_ms": 480.0, "task_class": "coding"}
    ] * 5)


def test_feed_records_measured_outcomes_on_frontier_only():
    table = RoutingTable(models=[])
    added = feed_routing_table(_report(), table, min_calls=5)
    # Only m1 is on the frontier with >=5 calls: 6 ok outcomes recorded.
    assert added == 6
    assert table.best_model("coding") == "m1"
    # m2 (dominated, off-frontier) received NO records — static preference
    # is not fabricated for measured-bad combos.
    rec = table._records.get("m2::coding")
    assert rec is None


def test_feed_all_combos_mode_records_failure_signal_too():
    table = RoutingTable(models=[])
    feed_routing_table(_report(), table, min_calls=5, only_frontier=False)
    m2 = table._records["m2::coding"]
    assert m2.attempts == 6 and m2.successes == 0


def test_min_calls_gate_skips_thin_signal():
    table = RoutingTable(models=[])
    report = build_report([
        {"model": "m1", "outcome": "ok", "task_class": "ops"},
    ])
    assert feed_routing_table(report, table, min_calls=5) == 0
    assert table.best_model("ops") is None


def test_run_feed_persists_round_trip(tmp_path, monkeypatch):
    monkeypatch.setenv("EVOLUTION_PROFILE_DIR", str(tmp_path))
    store = tmp_path / "calls.jsonl"
    store.write_text(
        "\n".join(json.dumps(r) for r in [
            {"model": "m1", "outcome": "ok", "cost": 0.01, "latency_ms": 50.0, "task_class": "eval"},
        ] * 6)
        + "\n",
        encoding="utf-8",
    )
    added = run_feed(store=store, table_path=tmp_path / "table.json")
    assert added == 6
    persisted = json.loads((tmp_path / "table.json").read_text())
    revived = RoutingTable.from_dict(persisted)
    assert revived.best_model("eval") == "m1"
    assert default_table_path() == tmp_path / "agentopt-routing-table.json"
