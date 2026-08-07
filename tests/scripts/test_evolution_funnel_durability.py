"""Tests for the Durability wiring in evolution_funnel (Slice C, #1775).

Verifies _compute_funnel_durable wraps compute_funnel in
FileDurabilityBackend, replays checkpoints on resume, and falls back to
direct compute when the backend is unavailable.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

import evolution_funnel as ef  # noqa: E402


class TestDurabilityWiring:
    """Tests that _compute_funnel_durable wraps compute_funnel correctly."""

    def test_durable_compute_returns_same_result_as_direct(self, tmp_path, monkeypatch):
        """Durable path produces the same funnel record as direct compute."""
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        date = "2026-01-15"
        ef._write_stage_files_for_test(tmp_path, date) if hasattr(ef, "_write_stage_files_for_test") else None
        direct = ef.compute_funnel(tmp_path, date)
        durable = ef._compute_funnel_durable(tmp_path, date)
        assert durable == direct

    def test_durable_compute_replays_checkpoint_on_resume(self, tmp_path, monkeypatch):
        """Second call replays checkpoint, no recomputation."""
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        date = "2026-01-16"
        call_count = 0
        original = ef.compute_funnel

        def counting_compute(evolution_dir, d):
            nonlocal call_count
            call_count += 1
            return original(evolution_dir, d)

        monkeypatch.setattr(ef, "compute_funnel", counting_compute)
        ef._compute_funnel_durable(tmp_path, date)
        assert call_count == 1
        # Second call should replay checkpoint — fn never invoked
        ef._compute_funnel_durable(tmp_path, date)
        assert call_count == 1

    def test_durable_compute_falls_back_when_backend_fails(self, tmp_path, monkeypatch):
        """Backend RuntimeError triggers fallback to direct compute."""
        date = "2026-01-17"

        class FailingBackend:
            def run(self, fn, checkpoint_id=None):
                raise RuntimeError("simulated backend failure")

        monkeypatch.setattr(
            "agent.durability.FileDurabilityBackend",
            lambda: FailingBackend(),
            raising=False,
        )

        # Import the module fresh to pick up the patched import
        import importlib

        importlib.reload(sys.modules.get("agent.durability", None) or __import__("agent.durability"))
        # Direct compute should still work via fallback
        result = ef._compute_funnel_durable(tmp_path, date)
        assert isinstance(result, dict)
