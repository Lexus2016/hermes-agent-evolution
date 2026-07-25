"""Integration test for the adversarial floor test wiring (#1267).

Verifies the floor test module is ACTUALLY INVOKED from its pipeline call site
(`evolution_funnel.py`'s `main()`), not just that the module works in isolation.
The previous PR (#1274) was closed as dead code — the module existed but nothing
called it. This test would have caught that: it mocks the floor test, runs the
funnel, and asserts the mock was called with the expected payload.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

# Make scripts/ importable so evolution_funnel can import evolution_adversarial_floor_test
_SCRIPTS_DIR = str(Path(__file__).resolve().parent.parent.parent / "scripts")
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)


def _make_minimal_evolution_dir(tmp_path: Path, date: str) -> Path:
    """Create a minimal evolution dir with enough stage reports for the funnel."""
    evo = tmp_path / "evolution"
    evo.mkdir()
    # issues report — minimal
    (evo / "issues" / f"{date}.json").parent.mkdir(parents=True)
    (evo / "issues" / f"{date}.json").write_text(
        json.dumps({"issues_created": [], "total_proposals": 0})
    )
    return evo


class TestFloorTestWiring:
    """The floor test must be called from the funnel's main(), not just exist."""

    def test_funnel_invokes_floor_test(self, tmp_path, monkeypatch):
        """Run the funnel and assert the floor test evaluate() is called."""
        import evolution_funnel

        date = "2026-07-25"
        evo_dir = _make_minimal_evolution_dir(tmp_path, date)
        monkeypatch.setenv("EVOLUTION_PROFILE_DIR", str(evo_dir))

        captured_payload = {}

        def fake_evaluate(payload):
            captured_payload.update(payload)
            return {
                "all_passed": True,
                "failed_metrics": [],
                "metric_results": [],
                "isolation": {"isolated": True},
                "judge_findings": [],
            }

        # Monkeypatch the gh merge-enrichment to avoid network calls.
        with patch.object(evolution_funnel, "_resolve_repo", return_value=None):
            with patch(
                "evolution_adversarial_floor_test.evaluate",
                side_effect=fake_evaluate,
            ):
                rc = evolution_funnel.main(["funnel", date])

        assert rc == 0, "funnel main() should exit 0"

        # The floor test was invoked from the funnel — not dead code.
        assert captured_payload, (
            "floor test evaluate() was never called from the funnel"
        )

        # The funnel passed the pipeline's metric specs.
        metrics = captured_payload.get("metrics", [])
        assert isinstance(metrics, list) and len(metrics) > 0, (
            "funnel must pass the pipeline's trusted metrics to the floor test"
        )
        metric_names = [m["name"] for m in metrics]
        assert "merge_success" in metric_names, (
            "merge_success must be in the floor-test payload — it is a trusted metric"
        )

        # The isolation check was included.
        iso = captured_payload.get("isolation")
        assert iso is not None, "isolation check must be in the floor-test payload"
        assert iso.get("verifier_context") != iso.get("implementer_context"), (
            "verifier and implementer contexts must differ (the SWE-bench isolation check)"
        )

    def test_funnel_writes_floor_test_report(self, tmp_path, monkeypatch):
        """The funnel must persist the floor-test report to floor-test/<date>.json."""
        import evolution_funnel

        date = "2026-07-25"
        evo_dir = _make_minimal_evolution_dir(tmp_path, date)
        monkeypatch.setenv("EVOLUTION_PROFILE_DIR", str(evo_dir))

        fake_report = {
            "all_passed": False,
            "failed_metrics": ["selection_count"],
            "metric_results": [
                {
                    "metric": "selection_count",
                    "strategy": "empty_patch",
                    "null_score": 1.0,
                    "floor": 0.0,
                    "ceiling": 1.0,
                    "passed": False,
                    "above_floor": True,
                }
            ],
            "isolation": {
                "verifier_context": "evolution-integration",
                "implementer_context": "evolution-implementation",
                "isolated": True,
                "reason": "ok",
            },
            "judge_findings": [],
        }

        with patch.object(evolution_funnel, "_resolve_repo", return_value=None):
            with patch(
                "evolution_adversarial_floor_test.evaluate",
                return_value=fake_report,
            ):
                evolution_funnel.main(["funnel", date])

        report_path = evo_dir / "floor-test" / f"{date}.json"
        assert report_path.exists(), (
            f"floor-test report not written to {report_path} — the wiring is incomplete"
        )
        written = json.loads(report_path.read_text())
        assert written == fake_report, (
            "written report must match what evaluate() returned"
        )

    def test_funnel_survives_floor_test_failure(self, tmp_path, monkeypatch):
        """If the floor test module raises, the funnel must NOT crash (fail-open)."""
        import evolution_funnel

        date = "2026-07-25"
        evo_dir = _make_minimal_evolution_dir(tmp_path, date)
        monkeypatch.setenv("EVOLUTION_PROFILE_DIR", str(evo_dir))

        with patch.object(evolution_funnel, "_resolve_repo", return_value=None):
            with patch(
                "evolution_adversarial_floor_test.evaluate",
                side_effect=RuntimeError("simulated floor-test crash"),
            ):
                rc = evolution_funnel.main(["funnel", date])

        assert rc == 0, (
            "funnel must exit 0 even if the floor test crashes — fail-open, never block metrics"
        )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
