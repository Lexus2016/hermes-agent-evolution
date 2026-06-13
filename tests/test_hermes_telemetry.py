"""Tests for hermes_telemetry.py — opt-in OpenTelemetry tracing (#167)."""

import pytest

import hermes_telemetry as tel

# OTel is an opt-in extra (pyproject `otel`); skip cleanly if it's absent.
pytest.importorskip("opentelemetry")
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult  # noqa: E402


class _Collect(SpanExporter):
    """Minimal in-memory exporter (version-robust — no dependency on the SDK's
    InMemorySpanExporter location)."""

    def __init__(self):
        self.spans = []

    def export(self, spans):
        self.spans.extend(spans)
        return SpanExportResult.SUCCESS

    def shutdown(self):
        pass


@pytest.fixture(autouse=True)
def _reset():
    tel._reset_for_test()
    yield
    tel._reset_for_test()


class TestDisabledIsNoOp:
    def test_span_yields_none_when_disabled(self, monkeypatch):
        monkeypatch.setattr(tel, "_otel_config", lambda: {})  # not enabled
        with tel.span("anything", tool="terminal") as sp:
            assert sp is None  # no-op
        assert tel.is_enabled() is False

    def test_disabled_span_runs_body_and_propagates_errors(self, monkeypatch):
        monkeypatch.setattr(tel, "_otel_config", lambda: {"enabled": False})
        ran = []
        with pytest.raises(ValueError):
            with tel.span("x"):
                ran.append(1)
                raise ValueError("boom")
        assert ran == [1]  # body executed; telemetry didn't swallow the error


class TestEnabled:
    def test_span_recorded_with_attributes(self):
        exp = _Collect()
        tel._force_enable_with_exporter(exp)
        with tel.span("cron.job", job="evolution-research", status="ok"):
            pass
        names = [s.name for s in exp.spans]
        assert "cron.job" in names
        attrs = dict(exp.spans[0].attributes)
        assert attrs.get("hermes.job") == "evolution-research"
        assert attrs.get("hermes.status") == "ok"

    def test_caller_exception_marks_span_error_and_propagates(self):
        exp = _Collect()
        tel._force_enable_with_exporter(exp)
        with pytest.raises(RuntimeError):
            with tel.span("agent.run"):
                raise RuntimeError("kaboom")
        # span still exported, with error status
        assert exp.spans and exp.spans[0].status.status_code.name == "ERROR"

    def test_telemetry_failure_never_breaks_caller(self):
        class _Broken(SpanExporter):
            def export(self, spans):
                raise RuntimeError("exporter down")

            def shutdown(self):
                pass

        tel._force_enable_with_exporter(_Broken())
        ran = []
        # A failing exporter must not raise into the caller.
        with tel.span("x"):
            ran.append(1)
        assert ran == [1]

    def test_set_attributes_on_current_span(self):
        exp = _Collect()
        tel._force_enable_with_exporter(exp)
        with tel.span("agent.run"):
            tel.set_attributes(tokens=123, provider="anthropic")
        attrs = dict(exp.spans[0].attributes)
        assert attrs.get("hermes.tokens") == 123
        assert attrs.get("hermes.provider") == "anthropic"
