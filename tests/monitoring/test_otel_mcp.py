"""OTel MCP tracing: a real tool call emits an execute_tool event whose
mcp_method_name maps to the OTel MCP attribute mcp.method.name."""

from __future__ import annotations

import threading
import types
from unittest.mock import MagicMock

from agent.monitoring import emitter as emitter_mod
from agent.monitoring.mcp_tracing import MCP_METHOD_NAME, mcp_span_attrs


def _patch_daemon_pool(monkeypatch):
    """Py3.14 workaround: daemon_pool mirrors pre-3.14 ThreadPoolExecutor
    internals; substitute a daemonized stdlib pool so the real concurrent
    tool-execution path runs unchanged."""
    import concurrent.futures

    import tools.daemon_pool as daemon_pool

    class _DaemonPool(concurrent.futures.ThreadPoolExecutor):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            for t in list(getattr(self, "_threads", ())):
                t.daemon = True

    monkeypatch.setattr(daemon_pool, "DaemonThreadPoolExecutor", _DaemonPool)


def test_live_tool_call_emits_execute_tool_event_with_mcp_method_name(monkeypatch):
    """A real concurrent tool call emits an ExecuteToolEvent with
    mcp_method_name mapped to mcp.method.name."""
    from run_agent import AIAgent

    _patch_daemon_pool(monkeypatch)

    em = emitter_mod.MonitoringEmitter()
    emitter_mod.reset_emitter_for_tests(em)
    received: list = []
    em.subscribe(lambda batch: received.extend(batch))
    try:
        agent = MagicMock()
        agent.session_id = "sess-e2e-0001"
        agent._interrupt_requested = False
        agent._tool_worker_threads = set()
        agent._tool_worker_threads_lock = threading.Lock()
        agent._invoke_tool = MagicMock(return_value="ok")
        agent._append_guardrail_observation = lambda name, args, result, failed: result
        agent._tool_result_content_for_active_model = lambda name, result: result
        agent._subdirectory_hints = MagicMock()
        agent._subdirectory_hints.check_tool_call.return_value = []
        agent.quiet_mode = True
        agent._should_emit_quiet_tool_messages = lambda: False
        agent._should_start_quiet_spinner = lambda: False
        agent._execute_tool_calls_concurrent = types.MethodType(
            AIAgent._execute_tool_calls_concurrent, agent
        )

        tc = MagicMock()
        tc.id = "call_e2e_1"
        tc.function.name = "read_file"
        tc.function.arguments = "{}"
        assistant_msg = MagicMock()
        assistant_msg.tool_calls = [tc]

        agent._execute_tool_calls_concurrent(assistant_msg, [], "task-e2e")
        em.flush()

        tool_events = [ev for ev in received if ev.get("event") == "execute_tool"]
        assert tool_events, f"no execute_tool event in {received!r}"
        ev = tool_events[0]
        assert ev["mcp_method_name"] == "read_file"
        assert ev["mcp_session_id"] == "sess-e2e-0001"
        assert ev["mcp_protocol_version"] == "2025-06-18"
        # The exporter seam: event field -> OTel MCP semconv attribute.
        assert mcp_span_attrs(ev)[MCP_METHOD_NAME] == "read_file"
        agent._invoke_tool.assert_called_once()
    finally:
        em.close()
        emitter_mod.reset_emitter_for_tests(None)


def test_mcp_attrs_absent_without_mcp_fields():
    # An execute_tool event with no MCP metadata must not pick up fake attrs.
    attrs = mcp_span_attrs({"event": "execute_tool", "name": "ls"})
    assert attrs == {}
