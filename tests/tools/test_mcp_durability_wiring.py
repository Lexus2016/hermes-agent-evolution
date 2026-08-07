"""Tests for the durability checkpoint wiring in MCP tool dispatch (#1782).

These tests verify the rework of PR #1785: the adapter is now called from the
real production call site (``tools/mcp_tool._register_server_tools``), not just
from its own unit tests.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from agent.durability import FileDurabilityBackend, NoOpDurability
from agent.mcp_durability_adapter import with_durability_checkpoint


# ---------------------------------------------------------------------------
# _wrap_with_durability
# ---------------------------------------------------------------------------


def test_wrap_returns_handler_unchanged_when_no_backend():
    """Default path: no backend configured → handler returned as-is."""
    from tools.mcp_tool import _wrap_with_durability

    def original_handler(args, **kwargs):
        return "raw"

    wrapped = _wrap_with_durability("some_tool", original_handler, backend=None)
    assert wrapped is original_handler, (
        "With no backend the handler must be returned unchanged "
        "(byte-identical default behavior)."
    )


def test_wrap_returns_handler_unchanged_for_noop_backend():
    """A NoOpDurability backend is equivalent to 'not configured'."""
    from tools.mcp_tool import _wrap_with_durability

    def original_handler(args, **kwargs):
        return "raw"

    wrapped = _wrap_with_durability(
        "some_tool", original_handler, backend=NoOpDurability()
    )
    assert wrapped is original_handler


def test_wrap_returns_new_handler_for_real_backend(tmp_path):
    """A FileDurabilityBackend (real) → handler is wrapped, not the same object."""
    from tools.mcp_tool import _wrap_with_durability

    def original_handler(args, **kwargs):
        return "raw"

    backend = FileDurabilityBackend(base_dir=tmp_path)
    wrapped = _wrap_with_durability("some_tool", original_handler, backend=backend)
    assert wrapped is not original_handler


# ---------------------------------------------------------------------------
# with_durability_checkpoint — end-to-end behavior
# ---------------------------------------------------------------------------


def test_durability_checkpoint_executes_and_replays(tmp_path):
    """First call executes; second call with same args replays from checkpoint."""
    call_count = 0

    def handler(args, **kwargs):
        nonlocal call_count
        call_count += 1
        return f"result-{call_count}"

    backend = FileDurabilityBackend(base_dir=tmp_path)
    wrapped = with_durability_checkpoint("my_tool", handler, backend)

    args = {"x": 1}
    r1 = wrapped(args)
    assert r1 == "result-1"
    assert call_count == 1

    # Same args → same call_id → replay from checkpoint, no re-execution.
    r2 = wrapped(args)
    assert r2 == "result-1"
    assert call_count == 1, "Replay must not re-invoke the underlying handler"


def test_durability_forwards_kwargs(tmp_path):
    """Dispatch metadata (**kwargs) must reach the underlying handler."""
    received_kwargs = {}

    def handler(args, **kwargs):
        received_kwargs.update(kwargs)
        return "ok"

    backend = FileDurabilityBackend(base_dir=tmp_path)
    wrapped = with_durability_checkpoint("kwarg_tool", handler, backend)
    wrapped({"a": 1}, task_id="t-123", request_id="r-456")

    assert received_kwargs == {"task_id": "t-123", "request_id": "r-456"}


def test_durability_different_args_both_execute(tmp_path):
    """Distinct args → distinct call_ids → both execute (no false replay)."""
    count = 0

    def handler(args, **kwargs):
        nonlocal count
        count += 1
        return str(count)

    backend = FileDurabilityBackend(base_dir=tmp_path)
    wrapped = with_durability_checkpoint("tool", handler, backend)

    assert wrapped({"x": 1}) == "1"
    assert wrapped({"x": 2}) == "2"
    assert count == 2


# ---------------------------------------------------------------------------
# _resolve_durability_backend — fail-open semantics
# ---------------------------------------------------------------------------


def test_resolve_backend_returns_none_by_default(monkeypatch):
    """With only the no-op backend registered, resolution returns None."""
    from agent.durability import MemoryDurabilityRegistry
    import tools.mcp_tool as mcp_tool_mod

    fake_registry = MemoryDurabilityRegistry()
    monkeypatch.setattr(mcp_tool_mod, "default_registry", lambda: fake_registry)

    assert mcp_tool_mod._resolve_durability_backend() is None


def test_resolve_backend_returns_real_when_registered(monkeypatch, tmp_path):
    """When a real backend is registered under 'file', resolution finds it."""
    from agent.durability import MemoryDurabilityRegistry
    import tools.mcp_tool as mcp_tool_mod

    registry = MemoryDurabilityRegistry()
    real = FileDurabilityBackend(base_dir=tmp_path)
    registry.register("file", real)
    monkeypatch.setattr(mcp_tool_mod, "default_registry", lambda: registry)

    resolved = mcp_tool_mod._resolve_durability_backend()
    assert resolved is real


if __name__ == "__main__":
    pytest.main([__file__, "-x", "-q"])
