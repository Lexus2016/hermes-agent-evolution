"""Wiring test: run_job emits a cron.job span when telemetry is enabled (#167).

Proves the thin run_job wrapper actually opens the span and stamps job
identity + success/error — without running the heavy _run_job_impl (stubbed).
"""

import pytest

pytest.importorskip("opentelemetry")
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult  # noqa: E402

import cron.scheduler as scheduler  # noqa: E402
import hermes_telemetry as tel  # noqa: E402


class _Collect(SpanExporter):
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


def test_run_job_emits_cron_job_span_on_success(monkeypatch):
    exp = _Collect()
    tel._force_enable_with_exporter(exp)
    monkeypatch.setattr(scheduler, "_run_job_impl", lambda job: (True, "out", "resp", None))

    ok, *_ = scheduler.run_job({"id": "evolution-research", "name": "Evolution: research"})

    assert ok is True
    span = next(s for s in exp.spans if s.name == "cron.job")
    attrs = dict(span.attributes)
    assert attrs.get("hermes.job") == "evolution-research"
    assert attrs.get("hermes.job_name") == "Evolution: research"
    assert attrs.get("hermes.success") is True


def test_run_job_span_records_failure(monkeypatch):
    exp = _Collect()
    tel._force_enable_with_exporter(exp)
    monkeypatch.setattr(scheduler, "_run_job_impl", lambda job: (False, "", "", "boom"))

    scheduler.run_job({"id": "evolution-issues"})

    span = next(s for s in exp.spans if s.name == "cron.job")
    attrs = dict(span.attributes)
    assert attrs.get("hermes.success") is False
    assert attrs.get("hermes.error") == "boom"


def test_run_job_disabled_is_noop(monkeypatch):
    # Default (disabled) telemetry: run_job works and emits nothing.
    monkeypatch.setattr(tel, "_otel_config", lambda: {})
    monkeypatch.setattr(scheduler, "_run_job_impl", lambda job: (True, "", "", None))
    ok, *_ = scheduler.run_job({"id": "x"})
    assert ok is True
    assert tel.is_enabled() is False
