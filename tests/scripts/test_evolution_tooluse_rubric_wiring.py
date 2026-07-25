"""Integration test for the tool-use competency rubric wiring (#1268).

Verifies the rubric module is ACTUALLY INVOKED from its pipeline call site
(`evolution_funnel.py`'s `main()`), not just that the module works in isolation.
The previous PR (#1276) was closed as dead code — the module existed but nothing
called it. This test would have caught that: it mocks the rubric, runs the
funnel, and asserts the mock was called with the cycle's tool-call traces.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

# Make scripts/ importable so evolution_funnel can import evolution_tooluse_rubric
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


def _make_trajectory_file(
    evo_dir: Path, date: str, session_id: str, entries: list
) -> Path:
    """Write a trajectory log file matching the funnel's scan pattern."""
    traj_dir = evo_dir / "trajectories"
    traj_dir.mkdir(parents=True, exist_ok=True)
    fname = f"{date}_{session_id}.json" if session_id else f"{date}.json"
    path = traj_dir / fname
    path.write_text(
        json.dumps({"date": date, "session_id": session_id, "entries": entries}),
        encoding="utf-8",
    )
    return path


class TestToolUseRubricWiring:
    """The tool-use rubric must be called from the funnel's main(), not just exist."""

    def test_funnel_invokes_rubric_with_trajectory_data(self, tmp_path, monkeypatch):
        """Run the funnel with trajectory files and assert the rubric is called."""
        import evolution_funnel

        date = "2026-07-25"
        evo_dir = _make_minimal_evolution_dir(tmp_path, date)
        monkeypatch.setenv("EVOLUTION_PROFILE_DIR", str(evo_dir))

        # Write trajectory files with tool-call entries
        _make_trajectory_file(
            evo_dir,
            date,
            "research",
            [
                {
                    "tool": "web_search",
                    "args_summary": {},
                    "result_status": "success",
                    "result_summary": "ok",
                },
                {
                    "tool": "read_file",
                    "args_summary": {"path": "test.py"},
                    "result_status": "failure",
                    "result_summary": "not found",
                },
                {
                    "tool": "read_file",
                    "args_summary": {"path": "test.py"},
                    "result_status": "success",
                    "result_summary": "ok",
                },
            ],
        )

        captured_payload = {}

        def fake_evaluate(payload):
            captured_payload.update(payload)
            return {
                "scores": {
                    "discovery": 0.8,
                    "parameterization": 0.9,
                    "syntax": 1.0,
                    "error_recovery": 0.7,
                    "efficiency": 0.85,
                    "overall": 0.85,
                },
                "repeated_call_clusters": [],
                "total_calls": 3,
                "unique_calls": 2,
            }

        with patch.object(evolution_funnel, "_resolve_repo", return_value=None):
            with patch(
                "evolution_tooluse_rubric.evaluate",
                side_effect=fake_evaluate,
            ):
                rc = evolution_funnel.main(["funnel", date])

        assert rc == 0, "funnel main() should exit 0"

        # The rubric was invoked from the funnel — not dead code.
        assert captured_payload, "rubric evaluate() was never called from the funnel"

        # The funnel passed the trajectory entries as tool calls.
        # The funnel also writes its own trajectory entry during execution, so
        # the calls list includes those entries too. We verify our test entries
        # are present (web_search, read_file) regardless of the funnel's own.
        calls = captured_payload.get("calls", [])
        assert isinstance(calls, list), "calls must be a list"
        assert len(calls) >= 3, (
            "funnel must pass at least the test trajectory's 3 tool calls to the rubric"
        )
        tool_names = [c["tool"] for c in calls]
        assert "web_search" in tool_names, (
            "web_search call from test trajectory must be present"
        )
        # Find the failed read_file call and verify succeeded=False
        _failed_rf = [
            c for c in calls if c["tool"] == "read_file" and not c["succeeded"]
        ]
        assert _failed_rf, "failed read_file call must have succeeded=False"

    def test_funnel_writes_rubric_report(self, tmp_path, monkeypatch):
        """The funnel must persist the rubric report to tooluse-rubric/<date>.json."""
        import evolution_funnel

        date = "2026-07-25"
        evo_dir = _make_minimal_evolution_dir(tmp_path, date)
        monkeypatch.setenv("EVOLUTION_PROFILE_DIR", str(evo_dir))

        _make_trajectory_file(
            evo_dir,
            date,
            "impl",
            [
                {
                    "tool": "terminal",
                    "args_summary": {},
                    "result_status": "success",
                    "result_summary": "",
                },
            ],
        )

        fake_report = {
            "scores": {
                "discovery": 1.0,
                "parameterization": 1.0,
                "syntax": 1.0,
                "error_recovery": 1.0,
                "efficiency": 1.0,
                "overall": 1.0,
            },
            "repeated_call_clusters": [],
            "total_calls": 1,
            "unique_calls": 1,
        }

        with patch.object(evolution_funnel, "_resolve_repo", return_value=None):
            with patch(
                "evolution_tooluse_rubric.evaluate",
                return_value=fake_report,
            ):
                evolution_funnel.main(["funnel", date])

        report_path = evo_dir / "tooluse-rubric" / f"{date}.json"
        assert report_path.exists(), (
            f"rubric report not written to {report_path} — the wiring is incomplete"
        )
        written = json.loads(report_path.read_text())
        assert written == fake_report, (
            "written report must match what evaluate() returned"
        )

    def test_funnel_survives_rubric_failure(self, tmp_path, monkeypatch):
        """If the rubric module raises, the funnel must NOT crash (fail-open)."""
        import evolution_funnel

        date = "2026-07-25"
        evo_dir = _make_minimal_evolution_dir(tmp_path, date)
        monkeypatch.setenv("EVOLUTION_PROFILE_DIR", str(evo_dir))

        with patch.object(evolution_funnel, "_resolve_repo", return_value=None):
            with patch(
                "evolution_tooluse_rubric.evaluate",
                side_effect=RuntimeError("simulated rubric crash"),
            ):
                rc = evolution_funnel.main(["funnel", date])

        assert rc == 0, (
            "funnel must exit 0 even if the rubric crashes — fail-open, never block metrics"
        )

    def test_funnel_handles_no_trajectories(self, tmp_path, monkeypatch):
        """With no trajectory files, the rubric still runs (fail-open, no crash)."""
        import evolution_funnel

        date = "2026-07-25"
        evo_dir = _make_minimal_evolution_dir(tmp_path, date)
        monkeypatch.setenv("EVOLUTION_PROFILE_DIR", str(evo_dir))
        # No trajectory files written by the test; the funnel may write its own.

        captured = {}

        def fake_evaluate(payload):
            captured.update(payload)
            return {"scores": {}, "total_calls": 0}

        with patch.object(evolution_funnel, "_resolve_repo", return_value=None):
            with patch(
                "evolution_tooluse_rubric.evaluate",
                side_effect=fake_evaluate,
            ):
                rc = evolution_funnel.main(["funnel", date])

        assert rc == 0
        # The rubric was called (wiring works) — calls may be empty or include
        # the funnel's own trajectory entry; the key is it didn't crash.
        assert "calls" in captured, "rubric must receive a calls key"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
