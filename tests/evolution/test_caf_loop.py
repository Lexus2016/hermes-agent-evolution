# -*- coding: utf-8 -*-
"""Tests for the C-A-F loop (Issue #2258, Slice B, parent #2247).

Behavior contracts: verifier pass/fail → experience records; regret math on
hand-built records; run_caf_cycle updates a real RoutingTable via
record_outcome; save/load round-trip; corrupt table → fail-open empty table.
"""

from __future__ import annotations

import json

import pytest

from evolution.lib.caf_loop import (
    CafExperience,
    CafRecord,
    CafSandboxVerifier,
    cumulative_regret,
    load_routing_table,
    run_caf_cycle,
    save_routing_table,
)
from tools.model_routing_table import RoutingTable


def _verifier_strict(task: str, answer: str) -> bool:
    return answer == "right"


class TestCafSandboxVerifier:
    def test_callable_verifier_pass_and_fail(self):
        v = CafSandboxVerifier()
        assert v.verify("t", "right", _verifier_strict) is True
        assert v.verify("t", "wrong", _verifier_strict) is False

    def test_callable_verifier_exception_is_fail(self):
        def boom(task: str, answer: str) -> bool:
            raise RuntimeError("boom")

        assert CafSandboxVerifier().verify("t", "x", boom) is False

    def test_script_verifier_exit_code_verdict(self, tmp_path):
        script = tmp_path / "verifier.py"
        script.write_text(
            "import json, sys\n"
            "payload = json.load(sys.stdin)\n"
            "sys.exit(0 if payload['answer'] == 'right' else 1)\n",
            encoding="utf-8",
        )
        v = CafSandboxVerifier()
        assert v.verify("t", "right", str(script)) is True
        assert v.verify("t", "wrong", str(script)) is False

    def test_script_verifier_missing_or_crashing_is_fail(self, tmp_path):
        v = CafSandboxVerifier()
        assert v.verify("t", "x", str(tmp_path / "missing.py")) is False
        crash = tmp_path / "crash.py"
        crash.write_text("raise SystemExit(2)\n", encoding="utf-8")
        assert v.verify("t", "x", str(crash)) is False


class TestCafExperience:
    def test_record_appends_and_loads_in_order(self, tmp_path):
        exp = CafExperience(tmp_path / "experience.jsonl")
        exp.record("coding", "model-a", True)
        exp.record("coding", "model-b", False)
        records = exp.load()
        assert [(r.task_dim, r.model, r.passed) for r in records] == [
            ("coding", "model-a", True),
            ("coding", "model-b", False),
        ]
        assert all(r.timestamp for r in records)

    def test_load_skips_corrupt_lines_and_missing_file(self, tmp_path):
        path = tmp_path / "experience.jsonl"
        path.write_text(
            '{"task_dim": "coding", "model": "m", "passed": true}\n'
            "not-json\n"
            "\n",
            encoding="utf-8",
        )
        records = CafExperience(path).load()
        assert len(records) == 1 and records[0].model == "m"
        assert CafExperience(tmp_path / "absent.jsonl").load() == []


class TestCumulativeRegret:
    def test_zero_for_best_model_and_unknown_model(self):
        records = [
            CafRecord("coding", "best", True, "t1"),
            CafRecord("coding", "best", False, "t2"),
            CafRecord("coding", "worse", False, "t3"),
        ]
        assert cumulative_regret(records, "best") == 0.0
        assert cumulative_regret(records, "stranger") == 0.0
        assert cumulative_regret([], "anyone") == 0.0

    def test_gap_to_per_dimension_best_model(self):
        records = [
            CafRecord("coding", "best", True, "t1"),
            CafRecord("coding", "best", False, "t2"),
            CafRecord("coding", "worse", False, "t3"),
            CafRecord("coding", "worse", False, "t4"),
        ]
        # best pass rate 0.5 vs worse 0.0 → regret 0.5
        assert cumulative_regret(records, "worse") == pytest.approx(0.5)

    def test_trial_weighted_across_dimensions(self):
        records = [
            # coding: model 2/2 (best), other 0/2 → gap 0.0 over 2 trials
            CafRecord("coding", "model", True, "1"),
            CafRecord("coding", "model", True, "2"),
            CafRecord("coding", "other", False, "3"),
            CafRecord("coding", "other", False, "4"),
            # reasoning: model 0/1, other 1/1 (best) → gap 1.0 over 1 trial
            CafRecord("reasoning", "model", False, "5"),
            CafRecord("reasoning", "other", True, "6"),
        ]
        assert cumulative_regret(records, "model") == pytest.approx(1 / 3)


class TestRunCafCycle:
    def test_records_experience_and_updates_routing_table(self, tmp_path):
        table = RoutingTable(models=["good", "bad"])
        exp = CafExperience(tmp_path / "experience.jsonl")

        report = run_caf_cycle(
            task_dim="coding",
            task="write a function",
            candidates={"good": "right", "bad": "wrong"},
            verifier=_verifier_strict,
            table=table,
            experience=exp,
        )

        # Experience store got the correct pass/fail records.
        by_model = {r.model: r.passed for r in exp.load()}
        assert by_model == {"good": True, "bad": False}
        assert all(r.task_dim == "coding" for r in exp.load())

        # RoutingTable was updated via record_outcome.
        recs = {r["model"]: r for r in table.to_dict()["records"]}
        assert recs["good"]["attempts"] == 1 and recs["good"]["successes"] == 1
        assert recs["bad"]["attempts"] == 1 and recs["bad"]["successes"] == 0
        assert table.best_model("coding") == "good"

        # Cycle report contract.
        assert report["task_dim"] == "coding"
        assert {r["model"]: r["passed"] for r in report["results"]} == {
            "good": True,
            "bad": False,
        }
        assert report["best_model"] == "good"
        assert report["regret"]["good"] == 0.0
        assert report["regret"]["bad"] == pytest.approx(1.0)

    def test_script_verifier_end_to_end(self, tmp_path):
        script = tmp_path / "verifier.py"
        script.write_text(
            "import json, sys\n"
            "payload = json.load(sys.stdin)\n"
            "sys.exit(0 if 'def ' in payload['answer'] else 1)\n",
            encoding="utf-8",
        )
        table = RoutingTable(models=["m1", "m2"])
        report = run_caf_cycle(
            task_dim="coding",
            task="t",
            candidates={"m1": "def f(): pass", "m2": "no code"},
            verifier=str(script),
            table=table,
            experience=CafExperience(tmp_path / "experience.jsonl"),
        )
        assert report["best_model"] == "m1"
        assert table.best_model("coding") == "m1"


class TestRoutingTablePersistence:
    def test_save_load_roundtrip(self, tmp_path):
        table = RoutingTable(models=["a", "b"], epsilon=0.25)
        table.record_outcome("a", "coding", True)
        table.record_outcome("b", "coding", False)
        path = tmp_path / "routing" / "table.json"
        assert save_routing_table(table, path) == path

        loaded = load_routing_table(path)
        assert isinstance(loaded, RoutingTable)
        assert loaded.models == ["a", "b"]
        assert loaded.epsilon == 0.25
        assert loaded.best_model("coding") == "a"

    def test_load_missing_file_returns_empty_table(self, tmp_path):
        loaded = load_routing_table(tmp_path / "absent.json")
        assert isinstance(loaded, RoutingTable)
        assert loaded.models == []

    def test_load_corrupt_file_returns_empty_table(self, tmp_path):
        path = tmp_path / "corrupt.json"
        path.write_text("{not json", encoding="utf-8")
        loaded = load_routing_table(path)
        assert isinstance(loaded, RoutingTable)
        assert loaded.models == []

    def test_load_structurally_invalid_returns_empty_table(self, tmp_path):
        path = tmp_path / "bad.json"
        path.write_text(
            json.dumps({"models": ["a"], "records": [{"nope": 1}]}),
            encoding="utf-8",
        )
        loaded = load_routing_table(path)
        assert loaded.models == []
