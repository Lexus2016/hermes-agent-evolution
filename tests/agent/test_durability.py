"""Tests for agent/durability.py — the composable Durability capability."""

from agent.durability import (
    Durability,
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
