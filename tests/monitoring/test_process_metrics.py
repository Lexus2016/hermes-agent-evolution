"""Process-level metrics + experience-reuse audit tests (stdlib + pytest)."""

from __future__ import annotations

import agent.monitoring.otlp_exporter as OE
from agent.monitoring.process_metrics import (
    ExperienceReuseAudit,
    audit_experience_reuse,
    process_attributes,
    reuse_attributes,
)


def test_process_attributes_map_normalized_scores_only():
    ev = {
        "solution_framing": 0.55,
        "execution": 0.92,
        "feedback_control": 0.81,
        "name": "x",
    }
    assert process_attributes(ev) == {
        "hermes.process.solution_framing": 0.55,
        "hermes.process.execution": 0.92,
        "hermes.process.feedback_control": 0.81,
    }
    # Out-of-range / non-numeric / missing are dropped.
    assert process_attributes({"solution_framing": 1.5}) == {}
    assert process_attributes({"solution_framing": "high"}) == {}
    assert process_attributes({"execution": True}) == {}


def test_reuse_attributes_surface_signal_only():
    assert reuse_attributes({
        "reuse": True,
        "count": 3,
        "source": "memory",
    }) == {
        "hermes.process.reuse": True,
        "hermes.process.reuse_count": 3,
        "hermes.process.reuse_source": "memory",
    }
    assert reuse_attributes({}) == {}
    assert reuse_attributes({"reuse": False}) == {}


def test_audit_counts_per_source_and_resets():
    a = ExperienceReuseAudit()
    assert a.record("memory")["count"] == 1
    assert a.record("memory")["count"] == 2
    assert a.record("other")["count"] == 1
    a.reset()
    assert a.record("memory")["count"] == 1


def test_audit_fail_closed_on_bad_input():
    # audit_experience_reuse never raises; no reuse_source means no signal.
    assert audit_experience_reuse({}) == {}
    assert audit_experience_reuse({"reuse_source": ""}) == {}


def test_span_attrs_include_process_metrics_when_present():
    event = {
        "event": "execute_tool",
        "tool": "terminal",
        "session_id": "s-a",
        "solution_framing": 0.6,
        "execution": 0.9,
        "feedback_control": 0.8,
        "reuse_source": "memory",
    }
    attrs = OE._span_attrs(event)
    assert attrs["hermes.process.solution_framing"] == 0.6
    assert attrs["hermes.process.execution"] == 0.9
    assert attrs["hermes.process.feedback_control"] == 0.8
    assert attrs["hermes.process.reuse"] is True
    assert attrs["hermes.process.reuse_source"] == "memory"


def test_span_attrs_without_process_fields_unchanged():
    attrs = OE._span_attrs({"event": "gateway_health", "name": "g.lifecycle"})
    assert attrs["hermes.event"] == "gateway_health"
    assert not any(k.startswith("hermes.process") for k in attrs)
