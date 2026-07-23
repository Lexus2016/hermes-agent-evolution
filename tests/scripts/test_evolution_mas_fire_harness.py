"""Tests for the MAS-FIRE coordination fault-injection harness (issue #1211)."""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from scripts.evolution_mas_fire_harness import (
    FAULT_TYPES,
    TIER_MECHANISM,
    TIER_NONE,
    classify_response,
    generate_report,
    inject_corrupted_summary,
    inject_instruction_drift,
    inject_stale_state,
    inject_truncated_summary,
    run_fault_injection_suite,
    SAMPLE_SUMMARY,
    main,
)


class TestInjectors:
    """Test each fault injection mechanism."""

    def test_stale_state_injection(self):
        result = inject_stale_state("clean data")
        assert "[STALE:" in result

    def test_corrupted_summary_injection(self):
        result = inject_corrupted_summary("This is a clean summary with words.")
        assert any(ord(c) < 0x20 and c not in "\n\r\t" for c in result)

    def test_truncated_summary_injection(self):
        result = inject_truncated_summary("A" * 100)
        assert len(result) <= 20

    def test_instruction_drift_injection(self):
        result = inject_instruction_drift("Found 3 functions in the module.")
        assert "Found 3 functions" not in result


class TestDetectors:
    """Test fault detection heuristics."""

    def test_detect_stale_state(self):
        assert (
            classify_response("stale_shared_state", "clean", "clean\n[STALE: x]")
            == TIER_MECHANISM
        )

    def test_detect_corruption(self):
        corrupted = inject_corrupted_summary("clean summary")
        assert (
            classify_response("corrupted_summary", "clean summary", corrupted)
            == TIER_MECHANISM
        )

    def test_detect_truncation(self):
        original = "A" * 100
        truncated = inject_truncated_summary(original)
        assert (
            classify_response("truncated_summary", original, truncated)
            == TIER_MECHANISM
        )

    def test_detect_instruction_drift(self):
        original = "Found 3 functions in the module with branching logic"
        drifted = inject_instruction_drift(original)
        assert (
            classify_response("instruction_drift", original, drifted) == TIER_MECHANISM
        )

    def test_no_fault_detected_for_clean_content(self):
        assert classify_response("corrupted_summary", "clean", "clean") == TIER_NONE


class TestSuiteRunner:
    """Test the full fault injection suite."""

    def test_runs_all_fault_types(self):
        results = run_fault_injection_suite()
        assert len(results) == len(FAULT_TYPES)
        fault_types_run = {r["fault_type"] for r in results}
        assert fault_types_run == set(FAULT_TYPES)

    def test_results_have_required_fields(self):
        results = run_fault_injection_suite()
        for r in results:
            assert "fault_type" in r
            assert "detected" in r
            assert "tier" in r
            assert "original_length" in r
            assert "received_length" in r

    def test_generate_report(self):
        results = run_fault_injection_suite()
        report = generate_report(results)
        assert report["total_faults"] == len(FAULT_TYPES)
        assert "detection_rate" in report
        assert "tier_distribution" in report
        assert "gaps" in report


class TestCLI:
    """Test the CLI runner."""

    def test_cli_run_stdout(self, capsys):
        rc = main(["run"])
        assert rc == 0
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert "results" in data
        assert "report" in data

    def test_cli_run_output_file(self, tmp_path):
        out = tmp_path / "report.json"
        rc = main(["run", "--output", str(out)])
        assert rc == 0
        assert out.exists()
        data = json.loads(out.read_text(encoding="utf-8"))
        assert "results" in data
