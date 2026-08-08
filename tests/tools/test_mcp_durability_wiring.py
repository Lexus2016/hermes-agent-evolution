"""Tests for MCP durability wiring (#1782).

Verifies that _wrap_with_durability returns the handler unchanged when no
real backend is configured (default, byte-identical behavior) and wraps the
handler when a real backend is injected.
"""

import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from agent.durability import FileDurabilityBackend, NoOpDurability
from tools.mcp_tool import _resolve_durability_backend, _wrap_with_durability


class TestWrapWithDurability:
    """Test the durability wrapping helper."""

    def test_no_backend_returns_handler_unchanged(self):
        """When backend=None (the default), the handler must be returned
        unchanged — no wrapping, byte-identical dispatch (#1782)."""
        original_handler = MagicMock(return_value="result")
        wrapped = _wrap_with_durability("test_tool", original_handler, backend=None)
        assert wrapped is original_handler, (
            "Handler must be returned unchanged when no backend is configured"
        )

    def test_noop_backend_returns_handler_unchanged(self):
        """A NoOpDurability backend also results in passthrough."""
        original_handler = MagicMock(return_value="result")
        wrapped = _wrap_with_durability(
            "test_tool", original_handler, backend=NoOpDurability()
        )
        assert wrapped is original_handler

    def test_none_backend_is_passthrough(self):
        """_resolve_durability_backend returns None when no real backend
        is configured — so the wrapping path should be a no-op."""
        original_handler = MagicMock(return_value="result")
        backend = _resolve_durability_backend()
        wrapped = _wrap_with_durability("test_tool", original_handler, backend=backend)
        assert wrapped is original_handler

    def test_wrapping_with_real_backend(self):
        """When a real (non-NoOp) backend is injected, the handler is wrapped
        with durability checkpointing."""
        with tempfile.TemporaryDirectory() as tmpdir:
            fake_backend = FileDurabilityBackend(base_dir=Path(tmpdir))
            original_handler = MagicMock(return_value="result")
            wrapped = _wrap_with_durability(
                "test_tool", original_handler, backend=fake_backend
            )
            assert wrapped is not original_handler, (
                "Handler must be wrapped when a real backend is configured"
            )

    def test_wrapped_handler_forwards_kwargs(self):
        """The wrapped handler must forward **kwargs (dispatch metadata like
        task_id) to the underlying tool function."""
        with tempfile.TemporaryDirectory() as tmpdir:
            fake_backend = FileDurabilityBackend(base_dir=Path(tmpdir))
            call_args = {}

            def mock_handler(args, **kwargs):
                call_args["kwargs"] = kwargs
                return "ok"

            wrapped = _wrap_with_durability(
                "test_tool", mock_handler, backend=fake_backend
            )
            result = wrapped(
                {"key": "value"}, task_id="test-task", session_id="sess-1"
            )
            assert result == "ok"
            assert call_args["kwargs"]["task_id"] == "test-task"
            assert call_args["kwargs"]["session_id"] == "sess-1"

    def test_wrapped_handler_returns_result(self):
        """The wrapped handler returns the tool's result string."""
        with tempfile.TemporaryDirectory() as tmpdir:
            fake_backend = FileDurabilityBackend(base_dir=Path(tmpdir))

            def mock_handler(args, **kwargs):
                return "tool result"

            wrapped = _wrap_with_durability(
                "test_tool", mock_handler, backend=fake_backend
            )
            assert wrapped({"x": 1}) == "tool result"

    def test_production_call_site_exists(self):
        """The wiring must exist in a production code path, not just tests.

        This is the core requirement of the #1782 rework: the adapter must
        be called from tools/mcp_tool.py, not just imported by tests.
        """
        import inspect

        from tools import mcp_tool

        assert hasattr(mcp_tool, "_wrap_with_durability")
        assert hasattr(mcp_tool, "_resolve_durability_backend")

        source = inspect.getsource(mcp_tool._register_server_tools)
        assert "_wrap_with_durability" in source, (
            "Production call site missing: _register_server_tools must call "
            "_wrap_with_durability to wire MCP tool calls through the "
            "durability backend (#1782)"
        )


class TestResolveDurabilityBackend:
    """Test backend resolution."""

    def test_returns_none_when_not_configured(self):
        """In a test environment with no durability configured, the resolver
        returns None — signaling 'no wrapping needed'."""
        backend = _resolve_durability_backend()
        if backend is not None:
            assert isinstance(backend, NoOpDurability), (
                f"Expected None or NoOpDurability, got {type(backend)}"
            )
