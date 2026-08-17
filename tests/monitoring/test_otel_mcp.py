"""OTel MCP tracing: execute_tool spans carry MCP semantic-convention attrs.

Uses the in-memory OTel span exporter (no network). Skipped when the optional
otlp extra is not installed.
"""

from __future__ import annotations

import pytest

otel = pytest.importorskip("opentelemetry.sdk.trace", reason="otlp extra not installed")

import agent.monitoring.otlp_exporter as OE
from agent.monitoring.events import ExecuteToolEvent
from agent.monitoring.mcp_tracing import (
    MCP_METHOD_NAME,
    MCP_PROTOCOL_VERSION,
    MCP_SESSION_ID,
    mcp_span_attrs,
)


def _mem_provider():
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return provider, exporter


def test_mcp_attrs_on_execute_tool_span():
    provider, mem = _mem_provider()
    ev = ExecuteToolEvent(
        name="mcp_tqmemory_recent_context",
        mcp_method_name="tools/call",
        mcp_session_id="sess-abc123",
        mcp_protocol_version="2025-06-18",
        status="completed",
        duration_ms=42,
    )
    n = OE.export_batch(provider, [ev.to_dict()])
    assert n == 1
    span = mem.get_finished_spans()[0]
    assert span.name == "hermes.execute_tool"
    attrs = dict(span.attributes or {})
    assert attrs[MCP_METHOD_NAME] == "tools/call"
    assert attrs[MCP_SESSION_ID] == "sess-abc123"
    assert attrs[MCP_PROTOCOL_VERSION] == "2025-06-18"


def test_mcp_attrs_absent_without_mcp_fields():
    # An execute_tool event with no MCP metadata must not pick up fake attrs.
    attrs = mcp_span_attrs({"event": "execute_tool", "name": "ls", "status": "ok"})
    assert attrs == {}


def test_emit_never_raises_for_malformed_execute_tool_event():
    from agent.monitoring.emitter import MonitoringEmitter

    provider, mem = _mem_provider()
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(OE, "_make_provider", lambda cfg: (provider, None))
    streamer = OE.OTLPStreamer({}, event_filter=lambda ev: True)

    em = MonitoringEmitter()
    em.subscribe(streamer)
    # Missing required field + non-dict payloads must never raise on emit.
    em.emit({"event": "execute_tool"})
    em.emit(object())
    em.flush()
    em.close()
    monkeypatch.undo()
