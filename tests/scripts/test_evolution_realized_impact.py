"""Tests for scripts/evolution_realized_impact.py — rate-based regression
comparison (#1324) and baseline rate storage at merge time.

The raw ``tool_failures`` count grows with session volume: 168 failures over
21 sessions (8.0/session) becomes 298 over 42 sessions (7.1/session = BETTER)
but a raw-count comparison calls it "regressed" because 298 > 168. Comparing
per-session rates instead eliminates this false regression. These tests cover
the pure ``compare_failure_rate`` function, ``record_merge`` with the optional
``baseline_tool_failure_rate`` field, and the ``compare-rate`` CLI subcommand.
"""

import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from evolution_realized_impact import (  # noqa: E402
    REGRESSION_THRESHOLD,
    compare_failure_rate,
    load_ledger,
    main,
    record_merge,
)


# ── compare_failure_rate (pure function) ────────────────────────────────────

class TestCompareFailureRate:
    def test_improved_when_rate_drops(self):
        baseline = {"terminal": 8.0}
        current = {"terminal": 7.1}
        result = compare_failure_rate(baseline, current)
        assert result["tools"]["terminal"]["verdict"] == "improved"
        assert "terminal" in result["improved_tools"]
        assert not result["any_regressed"]

    def test_regressed_when_rate_rises_above_threshold(self):
        # 8.0 -> 10.0 is a 25% increase (> 20% threshold)
        baseline = {"terminal": 8.0}
        current = {"terminal": 10.0}
        result = compare_failure_rate(baseline, current)
        assert result["tools"]["terminal"]["verdict"] == "regressed"
        assert "terminal" in result["regressed_tools"]
        assert result["any_regressed"]

    def test_stable_within_threshold(self):
        # 8.0 -> 9.0 is a 12.5% increase (< 20% threshold) → stable
        baseline = {"terminal": 8.0}
        current = {"terminal": 9.0}
        result = compare_failure_rate(baseline, current)
        assert result["tools"]["terminal"]["verdict"] == "stable"
        assert not result["any_regressed"]
        assert "terminal" not in result["improved_tools"]

    def test_false_regression_scenario_from_issue(self):
        """The core #1324 scenario: raw count rose (168→298) but per-session
        rate IMPROVED (8.0→7.1) because session volume grew ~1.8x. The rate
        comparison correctly says 'improved', not 'regressed'."""
        baseline = {"tool_call": 8.0}
        current = {"tool_call": 7.1}
        result = compare_failure_rate(baseline, current)
        assert result["tools"]["tool_call"]["verdict"] == "improved"
        assert not result["any_regressed"]

    def test_zero_baseline_with_current_failures_is_regressed(self):
        baseline = {"read_file": 0.0}
        current = {"read_file": 2.0}
        result = compare_failure_rate(baseline, current)
        assert result["tools"]["read_file"]["verdict"] == "regressed"
        assert "read_file" in result["regressed_tools"]

    def test_zero_baseline_zero_current_is_stable(self):
        baseline = {"read_file": 0.0}
        current = {"read_file": 0.0}
        result = compare_failure_rate(baseline, current)
        assert result["tools"]["read_file"]["verdict"] == "stable"

    def test_new_tool_appears_post_merge(self):
        baseline = {"terminal": 5.0}
        current = {"terminal": 5.0, "patch": 3.0}
        result = compare_failure_rate(baseline, current)
        assert result["tools"]["patch"]["verdict"] == "new"
        assert result["tools"]["terminal"]["verdict"] == "stable"

    def test_tool_disappears_post_merge(self):
        baseline = {"terminal": 5.0, "patch": 3.0}
        current = {"terminal": 5.0}
        result = compare_failure_rate(baseline, current)
        assert result["tools"]["patch"]["verdict"] == "gone"

    def test_none_inputs_return_empty(self):
        result = compare_failure_rate(None, {"terminal": 1.0})
        assert result == {
            "tools": {},
            "any_regressed": False,
            "regressed_tools": [],
            "improved_tools": [],
        }

    def test_empty_dicts_return_empty(self):
        result = compare_failure_rate({}, {})
        assert result["any_regressed"] is False
        assert result["tools"] == {}

    def test_multiple_tools_mixed_verdicts(self):
        baseline = {"terminal": 8.0, "read_file": 5.0, "patch": 2.0}
        current = {"terminal": 7.0, "read_file": 7.0, "patch": 2.1}
        result = compare_failure_rate(baseline, current)
        assert result["tools"]["terminal"]["verdict"] == "improved"
        assert result["tools"]["read_file"]["verdict"] == "regressed"
        assert result["tools"]["patch"]["verdict"] == "stable"
        assert result["any_regressed"]
        assert "read_file" in result["regressed_tools"]
        assert "terminal" in result["improved_tools"]

    def test_custom_threshold(self):
        # With a 10% threshold, 8.0 -> 9.0 (12.5%) is regressed
        baseline = {"terminal": 8.0}
        current = {"terminal": 9.0}
        result = compare_failure_rate(baseline, current, regression_threshold=0.10)
        assert result["tools"]["terminal"]["verdict"] == "regressed"

    def test_default_threshold_value(self):
        assert REGRESSION_THRESHOLD == 0.20


# ── record_merge with baseline_tool_failure_rate ────────────────────────────

class TestRecordMergeBaseline:
    def test_record_merge_with_baseline(self, tmp_path):
        ledger = tmp_path / "ledger.jsonl"
        baseline = {"terminal": 8.0, "read_file": 2.0}
        record_merge(ledger, 1234, "2026-07-27", 0.8, "fix terminal timeouts", baseline)
        records = load_ledger(ledger)
        assert len(records) == 1
        assert records[0]["issue"] == 1234
        assert records[0]["baseline_tool_failure_rate"] == baseline

    def test_record_merge_without_baseline(self, tmp_path):
        """Baseline is optional — old callers that don't pass it still work."""
        ledger = tmp_path / "ledger.jsonl"
        record_merge(ledger, 1234, "2026-07-27", 0.8, "fix something")
        records = load_ledger(ledger)
        assert len(records) == 1
        assert "baseline_tool_failure_rate" not in records[0]

    def test_baseline_survives_verdict_folding(self, tmp_path):
        """When a verdict is appended later, the merge metadata (including
        baseline_tool_failure_rate) must survive the fold."""
        ledger = tmp_path / "ledger.jsonl"
        baseline = {"terminal": 8.0}
        record_merge(ledger, 1234, "2026-07-27", 0.8, "fix", baseline)
        # Simulate a verdict line
        from evolution_realized_impact import record_verdict
        record_verdict(ledger, 1234, "confirmed", "2026-08-02", "rate dropped")
        records = load_ledger(ledger)
        assert len(records) == 1
        assert records[0]["verdict"] == "confirmed"
        assert records[0]["baseline_tool_failure_rate"] == baseline


# ── CLI compare-rate subcommand ─────────────────────────────────────────────

class TestCompareRateCLI:
    def test_compare_rate_prints_json(self, tmp_path, capsys):
        ledger = tmp_path / "ledger.jsonl"
        record_merge(ledger, 1234, "2026-07-27", 0.8, "fix", {"terminal": 8.0})
        evolution_dir = tmp_path  # ledger is at evolution_dir/realized/ledger.jsonl
        # But record_merge writes directly to the path given; load_ledger reads
        # from the path given. The CLI uses _evolution_dir(). We test main()
        # by pointing EVOLUTION_PROFILE_DIR to tmp_path and placing the ledger
        # at tmp_path/realized/ledger.jsonl.
        ledger_cli = tmp_path / "realized" / "ledger.jsonl"
        ledger_cli.parent.mkdir(parents=True)
        ledger_cli.write_text(ledger.read_text())
        os.environ["EVOLUTION_PROFILE_DIR"] = str(tmp_path)
        try:
            current_json = json.dumps({"terminal": 7.0})
            rc = main(["prog", "compare-rate", "1234", current_json])
            captured = capsys.readouterr()
            assert rc == 0
            result = json.loads(captured.out)
            assert result["tools"]["terminal"]["verdict"] == "improved"
        finally:
            del os.environ["EVOLUTION_PROFILE_DIR"]

    def test_compare_rate_no_baseline(self, tmp_path, capsys):
        """When no baseline exists (old merge record), returns empty result."""
        os.environ["EVOLUTION_PROFILE_DIR"] = str(tmp_path)
        try:
            (tmp_path / "realized").mkdir(parents=True)
            (tmp_path / "realized" / "ledger.jsonl").write_text("")
            current_json = json.dumps({"terminal": 5.0})
            rc = main(["prog", "compare-rate", "9999", current_json])
            captured = capsys.readouterr()
            assert rc == 0
            result = json.loads(captured.out)
            assert result["any_regressed"] is False
        finally:
            del os.environ["EVOLUTION_PROFILE_DIR"]

    def test_compare_rate_bad_json(self, tmp_path, capsys):
        os.environ["EVOLUTION_PROFILE_DIR"] = str(tmp_path)
        try:
            (tmp_path / "realized").mkdir(parents=True)
            (tmp_path / "realized" / "ledger.jsonl").write_text("")
            rc = main(["prog", "compare-rate", "1234", "not-json"])
            assert rc == 2
        finally:
            del os.environ["EVOLUTION_PROFILE_DIR"]


# ── record-merge CLI with baseline ──────────────────────────────────────────

class TestRecordMergeCLI:
    def test_record_merge_cli_with_baseline(self, tmp_path, capsys):
        os.environ["EVOLUTION_PROFILE_DIR"] = str(tmp_path)
        try:
            baseline_json = json.dumps({"terminal": 8.0})
            rc = main([
                "prog", "record-merge", "1234", "2026-07-27", "0.8",
                "fix terminal timeouts", baseline_json,
            ])
            assert rc == 0
            ledger = tmp_path / "realized" / "ledger.jsonl"
            records = load_ledger(ledger)
            assert records[0]["baseline_tool_failure_rate"] == {"terminal": 8.0}
        finally:
            del os.environ["EVOLUTION_PROFILE_DIR"]

    def test_record_merge_cli_without_baseline(self, tmp_path, capsys):
        os.environ["EVOLUTION_PROFILE_DIR"] = str(tmp_path)
        try:
            rc = main([
                "prog", "record-merge", "1234", "2026-07-27", "0.8",
                "fix terminal timeouts",
            ])
            assert rc == 0
            ledger = tmp_path / "realized" / "ledger.jsonl"
            records = load_ledger(ledger)
            assert "baseline_tool_failure_rate" not in records[0]
        finally:
            del os.environ["EVOLUTION_PROFILE_DIR"]

    def test_record_merge_cli_bad_baseline_ignored(self, tmp_path, capsys):
        """A malformed baseline JSON is warned about and ignored, but the merge
        is still recorded (without the baseline)."""
        os.environ["EVOLUTION_PROFILE_DIR"] = str(tmp_path)
        try:
            rc = main([
                "prog", "record-merge", "1234", "2026-07-27", "0.8",
                "fix terminal timeouts", "not-valid-json",
            ])
            assert rc == 0
            ledger = tmp_path / "realized" / "ledger.jsonl"
            records = load_ledger(ledger)
            assert "baseline_tool_failure_rate" not in records[0]
        finally:
            del os.environ["EVOLUTION_PROFILE_DIR"]