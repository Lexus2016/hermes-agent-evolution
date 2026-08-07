"""E2E tests for Slice B — wiring Durability into a real backend with resume."""

import os

from agent.durability import (
    DiskDurability,
    MemoryDurabilityRegistry,
    durable_run,
)


class _CountingSkill:
    """A stand-in skill whose expensive step is made durable via opt-in."""

    def __init__(self) -> None:
        self.runs = 0

    def step(self, value: int) -> dict:
        self.runs += 1
        return {"computed": value * 2}


def test_disk_backend_checkpoint_then_resume_without_reexec(tmp_path):
    """A checkpointed call resumes from disk after 'interruption' (no re-run)."""
    skill = _CountingSkill()
    registry = MemoryDurabilityRegistry()
    registry.register("disk", DiskDurability(checkpoint_dir=str(tmp_path)))

    first = durable_run(registry, "disk", lambda: skill.step(21), checkpoint_id="cp-1")
    assert first == {"computed": 42}
    assert skill.runs == 1

    # Simulate interruption: a NEW skill instance, same checkpoint id. The
    # backend must replay from disk without re-executing the callable.
    fresh = _CountingSkill()
    resumed = durable_run(
        registry, "disk", lambda: fresh.step(21), checkpoint_id="cp-1"
    )
    assert resumed == {"computed": 42}
    assert fresh.runs == 0  # never executed — replayed from checkpoint

    # A real checkpoint file exists under the temp HERMES_HOME checkpoint dir.
    assert os.path.exists(tmp_path / "cp-1.json")


def test_disk_backend_unknown_checkpoint_returns_none(tmp_path):
    """resume_from on a missing checkpoint returns None (nothing recorded)."""
    backend = DiskDurability(checkpoint_dir=str(tmp_path))
    assert backend.resume_from("missing") is None


def test_durable_run_with_noop_backend_still_executes(tmp_path):
    """A skill not opted in (noop) still runs, but is not checkpointed."""
    skill = _CountingSkill()
    registry = MemoryDurabilityRegistry()  # no backend registered -> noop default
    result = durable_run(
        registry, "does-not-exist", lambda: skill.step(5), checkpoint_id="cp-2"
    )
    assert result == {"computed": 10}
    assert skill.runs == 1
    assert not os.path.exists(tmp_path / "cp-2.json")
