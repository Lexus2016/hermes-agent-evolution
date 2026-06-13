"""Wiring test: run_conversation emits an agent.run span when telemetry is
enabled (#167).

Symmetric with tests/cron/test_cron_telemetry_span.py. Proves the thin
run_conversation wrapper opens the span, stamps task_id/provider/failed/
interrupted, and delegates to (a stubbed) _run_conversation_impl returning its
result verbatim — so a future refactor can't silently drop the span.
"""

import pytest

pytest.importorskip("opentelemetry")
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult  # noqa: E402

import agent.conversation_loop as cl  # noqa: E402
import hermes_telemetry as tel  # noqa: E402


class _Collect(SpanExporter):
    def __init__(self):
        self.spans = []

    def export(self, spans):
        self.spans.extend(spans)
        return SpanExportResult.SUCCESS

    def shutdown(self):
        pass


class _FakeAgent:
    provider = "anthropic"


@pytest.fixture(autouse=True)
def _reset():
    tel._reset_for_test()
    yield
    tel._reset_for_test()


def test_run_conversation_emits_agent_run_span(monkeypatch):
    exp = _Collect()
    tel._force_enable_with_exporter(exp)
    monkeypatch.setattr(
        cl,
        "_run_conversation_impl",
        lambda *a, **k: {"failed": False, "interrupted": False, "final_response": "ok"},
    )

    res = cl.run_conversation(_FakeAgent(), "hi", task_id="t-123")

    assert res["final_response"] == "ok"  # wrapper returns impl result verbatim
    span = next(s for s in exp.spans if s.name == "agent.run")
    attrs = dict(span.attributes)
    assert attrs.get("hermes.task_id") == "t-123"
    assert attrs.get("hermes.provider") == "anthropic"
    assert attrs.get("hermes.failed") is False
    assert attrs.get("hermes.interrupted") is False


def test_run_conversation_span_marks_failed_result(monkeypatch):
    exp = _Collect()
    tel._force_enable_with_exporter(exp)
    monkeypatch.setattr(
        cl,
        "_run_conversation_impl",
        lambda *a, **k: {"failed": True, "interrupted": False},
    )

    cl.run_conversation(_FakeAgent(), "hi", task_id="t-9")

    span = next(s for s in exp.spans if s.name == "agent.run")
    assert dict(span.attributes).get("hermes.failed") is True


def test_run_conversation_disabled_is_noop(monkeypatch):
    monkeypatch.setattr(tel, "_otel_config", lambda: {})
    monkeypatch.setattr(cl, "_run_conversation_impl", lambda *a, **k: {"final_response": "x"})
    res = cl.run_conversation(_FakeAgent(), "hi")
    assert res["final_response"] == "x"
    assert tel.is_enabled() is False
