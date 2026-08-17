"""Cost attribution + loop/redundant-call detector tests (stdlib + pytest)."""

from __future__ import annotations

import agent.monitoring.otlp_exporter as OE
from agent.monitoring.cost_attribution import (
    CostAnomalyDetector,
    anomaly_attributes,
    cost_attributes,
)


def test_cost_attributes_map_numeric_only():
    ev = {"prompt_tokens": 1200, "duration_ms": 812, "cost_usd": 0.0041, "name": "x"}
    assert cost_attributes(ev) == {
        "hermes.prompt_tokens": 1200,
        "hermes.duration_ms": 812,
        "hermes.cost_usd": 0.0041,
    }
    assert cost_attributes({"prompt_tokens": "n/a"}) == {}


def test_anomaly_attributes_surface_signal_only():
    assert anomaly_attributes({
        "anomaly": "retry_loop",
        "tool": "terminal",
        "count": 5,
    }) == {
        "hermes.cost_anomaly": "retry_loop",
        "hermes.anomaly_tool": "terminal",
        "hermes.anomaly_count": 5,
    }
    assert anomaly_attributes({}) == {}
    assert anomaly_attributes({"anomaly": ""}) == {}


def test_detector_fires_redundant_then_retry_loop():
    d = CostAnomalyDetector(redundant_threshold=3, retry_threshold=2)
    for _ in range(2):
        assert d.detect("terminal", "s1") == {}
    assert d.detect("terminal", "s1")["anomaly"] == "redundant_calls"
    d2 = CostAnomalyDetector(redundant_threshold=99, retry_threshold=2)
    assert d2.detect("web", "s1", status="error") == {}
    assert d2.detect("web", "s1", status="error")["anomaly"] == "retry_loop"


def test_detector_is_session_scoped_and_once_per_burst():
    d = CostAnomalyDetector(redundant_threshold=2, retry_threshold=99)
    assert d.detect("terminal", "s1") == {}
    assert d.detect("terminal", "s2") == {}  # different session, no fire
    assert d.detect("terminal", "s1")["anomaly"] == "redundant_calls"
    assert d.detect("terminal", "s1") == {}  # reset after firing


def test_span_attrs_include_cost_and_anomaly_when_present():
    event = {
        "event": "execute_tool",
        "tool": "terminal",
        "session_id": "s-a",
        "status": "error",
        "prompt_tokens": 900,
        "cost_usd": 0.002,
    }
    first = OE._span_attrs(event)  # default detector fires after 3 error calls
    assert first["hermes.prompt_tokens"] == 900
    assert "hermes.cost_anomaly" not in first
    for _ in range(2):
        OE._span_attrs(event)
    assert OE._span_attrs(event)["hermes.cost_anomaly"] == "retry_loop"


def test_span_attrs_without_cost_fields_unchanged():
    attrs = OE._span_attrs({"event": "gateway_health", "name": "g.lifecycle"})
    assert attrs["hermes.event"] == "gateway_health"
    assert not any(k.startswith("hermes.cost") for k in attrs)
