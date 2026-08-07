"""Tests for agent/durability.py — the composable Durability capability."""

import json
from pathlib import Path

from agent.durability import (
    Durability,
    FileDurabilityBackend,
    MemoryDurabilityRegistry,
    NoOpDurability,
)


class _RecordingBackend:
    """Standalone DurabilityBackend that records calls and can replay results."""

    def __init__(self, name: str = "recording") -> None:
        self.name = name
        self.runs = []
        self.resumes = []
        self._stored = {}

    def run(self, fn, checkpoint_id=None):  # noqa: ANN001
        self.runs.append(checkpoint_id)
        if checkpoint_id and checkpoint_id in self._stored:
            return self._stored[checkpoint_id]
        result = fn()
        if checkpoint_id is not None:
            self._stored[checkpoint_id] = result
        return result

    def resume_from(self, checkpoint_id):  # noqa: ANN001
        self.resumes.append(checkpoint_id)
        return self._stored.get(checkpoint_id)


def test_noop_default_and_unknown_backend_are_noop():
    """Default/unknown backends execute immediately and never checkpoint."""
    registry = MemoryDurabilityRegistry()
    backend = registry.resolve(None)
    assert isinstance(backend, NoOpDurability)
    assert isinstance(registry.resolve("temporal"), NoOpDurability)
    calls = []
    assert backend.run(lambda: calls.append(1) or "value") == "value"
    assert calls == [1]  # executed exactly once
    assert backend.resume_from("any") is None


def test_registry_register_get_roundtrip():
    """Registering a backend and fetching it by name returns the same object."""
    registry = MemoryDurabilityRegistry()
    backend = _RecordingBackend("temporal")
    registry.register("temporal", backend)
    assert registry.get("temporal") is backend
    assert registry.resolve("temporal") is backend
    assert registry.available() == {"temporal": "temporal"}


def test_opt_in_resolves_configured_backend():
    """A skill opting into a named backend gets that backend, per-capability."""
    registry = MemoryDurabilityRegistry()
    registry.register("dbos", _RecordingBackend("dbos"))
    cap = Durability(backend=registry.resolve("dbos"), enabled=True)
    assert cap.enabled is True
    assert cap.backend.name == "dbos"
    assert not isinstance(cap.backend, NoOpDurability)


def test_durability_is_per_skill_composable():
    """Capability attaches per-skill: one opted in, another not."""
    registry = MemoryDurabilityRegistry()
    registry.register("temporal", _RecordingBackend("temporal"))
    opted_in = Durability(backend=registry.resolve("temporal"), enabled=True)
    not_opted = Durability()  # default: disabled no-op
    assert opted_in.enabled is True
    assert not_opted.enabled is False
    assert isinstance(not_opted.backend, NoOpDurability)
    calls = []
    assert not_opted.backend.run(lambda: calls.append(1) or "x", "cp-1") == "x"
    assert calls == [1]


def test_checkpoint_resume_roundtrip():
    """A backend stores a result under a checkpoint id and replays it."""
    backend = _RecordingBackend()
    assert backend.run(lambda: {"step": "done"}, checkpoint_id="cp-1") == {
        "step": "done"
    }
    assert backend.resume_from("cp-1") == {"step": "done"}
    assert _RecordingBackend().resume_from("cp-9") is None


# ── FileDurabilityBackend — real filesystem checkpoint/resume (Slice B #1762) ──


def test_file_backend_checkpoint_and_resume(tmp_path: Path):
    """E2E: FileDurabilityBackend persists to disk and resumes from checkpoint."""
    backend = FileDurabilityBackend(base_dir=tmp_path)
    call_count = 0

    def costly_computation():
        nonlocal call_count
        call_count += 1
        return {"findings": ["a", "b"], "cycles": 3}

    # First run: executes fn, writes checkpoint
    result = backend.run(costly_computation, checkpoint_id="research-2026-08-07")
    assert result == {"findings": ["a", "b"], "cycles": 3}
    assert call_count == 1  # executed once

    # Checkpoint file exists on disk (real filesystem, not mock)
    cp_file = tmp_path / "research-2026-08-07.json"
    assert cp_file.exists()
    assert json.loads(cp_file.read_text())["findings"] == ["a", "b"]

    # Resume: returns checkpointed result WITHOUT re-executing fn
    resumed = backend.resume_from("research-2026-08-07")
    assert resumed == {"findings": ["a", "b"], "cycles": 3}
    assert call_count == 1  # still 1 — no re-execution

    # Second run with same checkpoint_id: idempotent re-entry, skips fn
    result2 = backend.run(costly_computation, checkpoint_id="research-2026-08-07")
    assert result2 == {"findings": ["a", "b"], "cycles": 3}
    assert call_count == 1  # NOT incremented — checkpoint replayed


def test_file_backend_resume_unknown_returns_none(tmp_path: Path):
    """resume_from a non-existent checkpoint returns None."""
    backend = FileDurabilityBackend(base_dir=tmp_path)
    assert backend.resume_from("does-not-exist") is None


def test_file_backend_no_checkpoint_id_executes_directly(tmp_path: Path):
    """Without a checkpoint_id, fn runs directly (no persistence)."""
    backend = FileDurabilityBackend(base_dir=tmp_path)
    calls = []
    result = backend.run(lambda: calls.append(1) or "value")
    assert result == "value"
    assert calls == [1]
    assert not (tmp_path / "None.json").exists()


def test_file_backend_corrupted_checkpoint_recomputes(tmp_path: Path):
    """A corrupted checkpoint file is ignored and fn recomputes."""
    backend = FileDurabilityBackend(base_dir=tmp_path)
    cp_file = tmp_path / "corrupt.json"
    cp_file.write_text("NOT VALID JSON {{{{", encoding="utf-8")
    calls = []
    result = backend.run(lambda: calls.append(1) or {"fresh": True}, "corrupt")
    assert result == {"fresh": True}
    assert calls == [1]  # recomputed because checkpoint was unreadable


def test_file_backend_path_traversal_sanitized(tmp_path: Path):
    """Checkpoint IDs with path separators are sanitized (no traversal)."""
    backend = FileDurabilityBackend(base_dir=tmp_path)
    backend.run(lambda: "ok", checkpoint_id="../../etc/passwd")
    # Should have written a sanitized file, NOT traversed directories
    safe_files = list(tmp_path.glob("*.json"))
    assert len(safe_files) == 1
    assert "/" not in safe_files[0].name
    # The file must be inside the base_dir — no directory escape happened
    assert safe_files[0].parent.resolve() == tmp_path.resolve()


def test_file_backend_registered_in_default_registry():
    """FileDurabilityBackend can be registered in MemoryDurabilityRegistry."""
    registry = MemoryDurabilityRegistry()
    backend = FileDurabilityBackend()
    registry.register("file", backend)
    resolved = registry.resolve("file")
    assert resolved is backend
    assert resolved.name == "file"
