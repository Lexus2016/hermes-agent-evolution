"""Integration test for the selective memory gate wiring (#1270).

Verifies the memory gate module is ACTUALLY INVOKED from its pipeline call site
(`evolution_funnel.py`'s `main()`), not just that the module works in isolation.
The previous PR (#1275) was closed as dead code — the module existed but nothing
called it. This test would have caught that: it mocks the memory gate, runs the
funnel, and asserts the mock was called with the expected payload.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

# Make scripts/ importable so evolution_funnel can import evolution_memory_gate
_SCRIPTS_DIR = str(Path(__file__).resolve().parent.parent.parent / "scripts")
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)


def _make_minimal_evolution_dir(tmp_path: Path, date: str) -> Path:
    """Create a minimal evolution dir with enough stage reports for the funnel."""
    evo = tmp_path / "evolution"
    evo.mkdir()
    (evo / "issues" / f"{date}.json").parent.mkdir(parents=True)
    (evo / "issues" / f"{date}.json").write_text(
        json.dumps({"issues_created": [], "total_proposals": 0})
    )
    return evo


def _make_analysis_with_selected(date: str) -> dict:
    """Build a minimal analysis report with selected proposals."""
    return {
        "date": date,
        "selected_for_implementation": [
            {
                "issue_number": 100,
                "title": "High-impact feature",
                "impact_score": 0.8,
            },
            {
                "issue_number": 101,
                "title": "Low-impact feature",
                "impact_score": 0.2,
            },
        ],
    }


class TestMemoryGateWiring:
    """The memory gate must be called from the funnel's main(), not just exist."""

    def test_funnel_invokes_memory_gate(self, tmp_path, monkeypatch):
        """Run the funnel and assert the memory gate evaluate() is called."""
        import evolution_funnel

        date = "2026-07-25"
        evo_dir = _make_minimal_evolution_dir(tmp_path, date)
        monkeypatch.setenv("EVOLUTION_PROFILE_DIR", str(evo_dir))

        # Write the analysis report so the funnel can read selected proposals
        analysis = _make_analysis_with_selected(date)
        (evo_dir / "analysis" / f"{date}.json").parent.mkdir(parents=True)
        (evo_dir / "analysis" / f"{date}.json").write_text(json.dumps(analysis))

        captured_payload = {}

        def fake_evaluate(payload):
            captured_payload.update(payload)
            return {
                "summary": {
                    "admitted": 1,
                    "rejected": 1,
                    "error_propagation_blocks": 0,
                    "deletion_candidates": 0,
                    "misaligned_flags": 0,
                },
                "addition_decisions": [],
                "deletion_candidates": [],
                "misaligned_flags": [],
                "retrieval_events": [],
            }

        with patch.object(evolution_funnel, "_resolve_repo", return_value=None):
            with patch(
                "evolution_memory_gate.evaluate",
                side_effect=fake_evaluate,
            ):
                rc = evolution_funnel.main(["funnel", date])

        assert rc == 0, "funnel main() should exit 0"

        # The memory gate was invoked from the funnel — not dead code.
        assert captured_payload, (
            "memory gate evaluate() was never called from the funnel"
        )

        # The funnel passed the cycle's selected proposals as memory records.
        records = captured_payload.get("records", [])
        assert isinstance(records, list) and len(records) == 2, (
            "funnel must pass the selected proposals as memory records to the gate"
        )
        record_ids = [r["record_id"] for r in records]
        assert "issue-100" in record_ids, "high-impact proposal must be in the payload"
        assert "issue-101" in record_ids, "low-impact proposal must be in the payload"

        # Quality threshold is set
        assert "quality_threshold" in captured_payload, "quality_threshold must be set"

    def test_funnel_writes_memory_gate_report(self, tmp_path, monkeypatch):
        """The funnel must persist the memory-gate report to memory-gate/<date>.json."""
        import evolution_funnel

        date = "2026-07-25"
        evo_dir = _make_minimal_evolution_dir(tmp_path, date)
        monkeypatch.setenv("EVOLUTION_PROFILE_DIR", str(evo_dir))

        analysis = _make_analysis_with_selected(date)
        (evo_dir / "analysis" / f"{date}.json").parent.mkdir(parents=True)
        (evo_dir / "analysis" / f"{date}.json").write_text(json.dumps(analysis))

        fake_report = {
            "summary": {
                "admitted": 1,
                "rejected": 1,
                "error_propagation_blocks": 0,
                "deletion_candidates": 0,
                "misaligned_flags": 0,
            },
            "addition_decisions": [
                {"record_id": "issue-100", "admitted": True},
                {"record_id": "issue-101", "admitted": False},
            ],
            "deletion_candidates": [],
            "misaligned_flags": [],
            "retrieval_events": [],
        }

        with patch.object(evolution_funnel, "_resolve_repo", return_value=None):
            with patch(
                "evolution_memory_gate.evaluate",
                return_value=fake_report,
            ):
                evolution_funnel.main(["funnel", date])

        report_path = evo_dir / "memory-gate" / f"{date}.json"
        assert report_path.exists(), (
            f"memory-gate report not written to {report_path} — the wiring is incomplete"
        )
        written = json.loads(report_path.read_text())
        assert written == fake_report, (
            "written report must match what evaluate() returned"
        )

    def test_funnel_survives_memory_gate_failure(self, tmp_path, monkeypatch):
        """If the memory gate module raises, the funnel must NOT crash (fail-open)."""
        import evolution_funnel

        date = "2026-07-25"
        evo_dir = _make_minimal_evolution_dir(tmp_path, date)
        monkeypatch.setenv("EVOLUTION_PROFILE_DIR", str(evo_dir))

        with patch.object(evolution_funnel, "_resolve_repo", return_value=None):
            with patch(
                "evolution_memory_gate.evaluate",
                side_effect=RuntimeError("simulated memory-gate crash"),
            ):
                rc = evolution_funnel.main(["funnel", date])

        assert rc == 0, (
            "funnel must exit 0 even if the memory gate crashes — fail-open, never block metrics"
        )

    def test_funnel_handles_missing_analysis(self, tmp_path, monkeypatch):
        """If the analysis report is missing, the gate runs with zero records (no crash)."""
        import evolution_funnel

        date = "2026-07-25"
        evo_dir = _make_minimal_evolution_dir(tmp_path, date)
        monkeypatch.setenv("EVOLUTION_PROFILE_DIR", str(evo_dir))
        # No analysis report written

        captured = {}

        def fake_evaluate(payload):
            captured.update(payload)
            return {"summary": {}, "addition_decisions": []}

        with patch.object(evolution_funnel, "_resolve_repo", return_value=None):
            with patch(
                "evolution_memory_gate.evaluate",
                side_effect=fake_evaluate,
            ):
                rc = evolution_funnel.main(["funnel", date])

        assert rc == 0
        assert captured.get("records") == [], (
            "with no analysis report, the gate should receive an empty records list"
        )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
