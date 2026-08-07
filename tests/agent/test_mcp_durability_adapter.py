"""Tests for the durability-aware MCP adapter (Slice C, #1782 / #1288).

Tests the checkpoint / replay lifecycle of :class:`McpDurabilityAdapter`
against both :class:`NoOpDurability` (passthrough) and
:class:`FileDurabilityBackend` (real checkpoint files), plus the
:func:`with_durability_checkpoint` convenience wrapper.
"""

from __future__ import annotations

from pathlib import Path

from agent.durability import FileDurabilityBackend, NoOpDurability
from agent.mcp_durability_adapter import (
    McpDurabilityAdapter,
    _checkpoint_id,
    with_durability_checkpoint,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class RecordingBackend:
    """Minimal DurabilityBackend that records calls in-memory."""

    name = "recording"

    def __init__(self) -> None:
        self.checkpoints: dict[str, object] = {}

    def run(self, fn, checkpoint_id=None):
        if checkpoint_id is not None and checkpoint_id in self.checkpoints:
            return self.checkpoints[checkpoint_id]
        result = fn()
        if checkpoint_id is not None:
            self.checkpoints[checkpoint_id] = result
        return result

    def resume_from(self, checkpoint_id: str):
        return self.checkpoints.get(checkpoint_id)


def _make_fn(result: str = "ok", calls: list | None = None):
    """Return a tool_fn that records its invocations."""

    def _fn(args: dict) -> str:
        if calls is not None:
            calls.append(args)
        return result

    return _fn


# ---------------------------------------------------------------------------
# _checkpoint_id
# ---------------------------------------------------------------------------


class TestCheckpointId:
    def test_deterministic(self):
        assert _checkpoint_id("foo", "abc") == _checkpoint_id("foo", "abc")

    def test_different_inputs_differ(self):
        assert _checkpoint_id("foo", "abc") != _checkpoint_id("bar", "abc")
        assert _checkpoint_id("foo", "abc") != _checkpoint_id("foo", "def")

    def test_prefix(self):
        assert _checkpoint_id("foo", "abc").startswith("mcp-tool-call-")


# ---------------------------------------------------------------------------
# NoOp backend — passthrough
# ---------------------------------------------------------------------------


class TestNoOpPassthrough:
    def test_executes_and_returns_result(self):
        fn = _make_fn("hello")
        adapter = McpDurabilityAdapter("search", fn, backend=NoOpDurability())
        result = adapter.call({"q": "test"}, "call-1")
        assert result == "hello"

    def test_re_executes_on_replay_with_noop(self):
        """NoOp stores nothing, so replay always re-executes."""
        calls: list = []
        fn = _make_fn("ok", calls)
        adapter = McpDurabilityAdapter("search", fn, backend=NoOpDurability())
        adapter.call({"q": "a"}, "call-1")
        adapter.call({"q": "a"}, "call-1")
        assert len(calls) == 2  # no caching


# ---------------------------------------------------------------------------
# Real backend — checkpoint + replay
# ---------------------------------------------------------------------------


class TestReplayWithRecordingBackend:
    def test_replay_returns_cached_result(self):
        calls: list = []
        fn = _make_fn("result-data", calls)
        backend = RecordingBackend()
        adapter = McpDurabilityAdapter("lookup", fn, backend=backend)

        first = adapter.call({"id": 42}, "call-1")
        assert first == "result-data"
        assert len(calls) == 1

        # Replay with same call_id → cached, no re-execution.
        second = adapter.call({"id": 42}, "call-1")
        assert second == "result-data"
        assert len(calls) == 1

    def test_different_call_id_re_executes(self):
        calls: list = []
        fn = _make_fn("ok", calls)
        backend = RecordingBackend()
        adapter = McpDurabilityAdapter("lookup", fn, backend=backend)

        adapter.call({"id": 1}, "call-A")
        adapter.call({"id": 2}, "call-B")
        assert len(calls) == 2

    def test_checkpointed_pending_then_done(self):
        fn = _make_fn("done-val")
        backend = RecordingBackend()
        adapter = McpDurabilityAdapter("write", fn, backend=backend)
        adapter.call({"x": 1}, "call-1")

        # The done checkpoint must exist.
        cp_id = _checkpoint_id("write", "call-1")
        done = backend.resume_from(f"{cp_id}-done")
        assert isinstance(done, dict)
        assert done["status"] == "done"
        assert done["result"] == "done-val"
        assert done["tool_name"] == "write"


# ---------------------------------------------------------------------------
# FileDurabilityBackend — end-to-end with real files
# ---------------------------------------------------------------------------


class TestFileBackendIntegration:
    def test_crash_recovery_replays_from_file(self, tmp_path: Path):
        backend = FileDurabilityBackend(base_dir=tmp_path)
        calls: list = []
        fn = _make_fn("persisted", calls)

        adapter = McpDurabilityAdapter("fetch", fn, backend=backend)
        result = adapter.call({"url": "http://x"}, "call-99")
        assert result == "persisted"
        assert len(calls) == 1

        # Simulate crash: new adapter + backend with same dir, same call_id.
        backend2 = FileDurabilityBackend(base_dir=tmp_path)
        fn2 = _make_fn("SHOULD-NOT-CALL", calls)
        adapter2 = McpDurabilityAdapter("fetch", fn2, backend=backend2)
        result2 = adapter2.call({"url": "http://x"}, "call-99")
        assert result2 == "persisted"
        assert len(calls) == 1  # fn2 was never called


# ---------------------------------------------------------------------------
# with_durability_checkpoint wrapper
# ---------------------------------------------------------------------------


class TestWithDurabilityCheckpoint:
    def test_wrapper_signature_matches_handler(self):
        fn = _make_fn("wrapped-ok")
        wrapped = with_durability_checkpoint("tool", fn)
        result = wrapped({"arg": 1})
        assert result == "wrapped-ok"

    def test_wrapper_replays_identical_args(self):
        calls: list = []
        fn = _make_fn("v1", calls)
        backend = RecordingBackend()
        wrapped = with_durability_checkpoint("tool", fn, backend=backend)

        r1 = wrapped({"arg": 1})
        r2 = wrapped({"arg": 1})
        assert r1 == r2 == "v1"
        assert len(calls) == 1  # second was replayed

    def test_wrapper_different_args_re_execute(self):
        calls: list = []
        fn = _make_fn("v1", calls)
        wrapped = with_durability_checkpoint("tool", fn, backend=RecordingBackend())
        wrapped({"arg": 1})
        wrapped({"arg": 2})
        assert len(calls) == 2
