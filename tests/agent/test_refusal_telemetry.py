"""Tests for the refusal nudge telemetry sidecar (#1265).

Covers ``record_nudge`` / ``record_transition`` / ``load_events`` /
``summarize`` against a temp HERMES_HOME, ``detect_refusal_category``, and
best-effort guarantees (broken sidecar never raises).
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from agent import refusal_telemetry
from agent.loop_guard import detect_refusal_category


@pytest.fixture(autouse=True)
def _tmp_telemetry_home(tmp_path, monkeypatch):
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setattr("agent.refusal_telemetry.get_hermes_home", lambda: hermes_home)
    yield hermes_home


# ── detect_refusal_category ───────────────────────────────────────────────────


class TestDetectRefusalCategory:
    def test_over_refusal_detected(self):
        text = "I can't do that — it would require accessing a system I don't have."
        assert detect_refusal_category(text) == "over_refusal"

    def test_no_refusal_returns_empty(self):
        assert detect_refusal_category("Sure! I'll create the file now.") == ""

    def test_empty_text_returns_empty(self):
        assert detect_refusal_category("") == ""
        assert detect_refusal_category("   ") == ""

    def test_returns_known_category_or_empty(self):
        text = "I don't have access to the database, so I can't query the records."
        cat = detect_refusal_category(text)
        assert cat == "" or cat in ("true_capability_gap", "over_refusal", "permission_boundary")


# ── record_nudge / record_transition ──────────────────────────────────────────


class TestRecording:
    def test_nudge_recorded(self, _tmp_telemetry_home):
        refusal_telemetry.record_nudge(
            session_id="sess-1", nudge_tier="advisory",
            refusal_category="over_refusal", nudge_count=1, session_refusal_count=1,
        )
        events = refusal_telemetry.load_events()
        assert len(events) == 1
        e = events[0]
        assert e["type"] == "nudge"
        assert e["nudge_tier"] == "advisory"
        assert e["session_id"] == "sess-1"
        assert "timestamp" in e

    def test_none_session_id_becomes_empty(self, _tmp_telemetry_home):
        refusal_telemetry.record_nudge(
            session_id=None, nudge_tier="directive",
            refusal_category="over_refusal", nudge_count=2, session_refusal_count=3,
        )
        assert refusal_telemetry.load_events()[0]["session_id"] == ""

    def test_transition_recorded(self, _tmp_telemetry_home):
        refusal_telemetry.record_transition(
            session_id="s1", nudge_tier="advisory",
            category_before="over_refusal", category_after="",
            recovered=True, took_action=True,
        )
        e = refusal_telemetry.load_events()[0]
        assert e["type"] == "transition"
        assert e["recovered"] is True
        assert e["took_action"] is True

    def test_mixed_events_accumulate(self, _tmp_telemetry_home):
        refusal_telemetry.record_nudge(
            session_id="s1", nudge_tier="advisory",
            refusal_category="over_refusal", nudge_count=1, session_refusal_count=1,
        )
        refusal_telemetry.record_transition(
            session_id="s1", nudge_tier="advisory",
            category_before="over_refusal", category_after="",
            recovered=True, took_action=True,
        )
        events = refusal_telemetry.load_events()
        assert len(events) == 2
        assert events[0]["type"] == "nudge"
        assert events[1]["type"] == "transition"

    def test_sidecar_file_format(self, _tmp_telemetry_home):
        refusal_telemetry.record_nudge(
            session_id="s1", nudge_tier="advisory",
            refusal_category="over_refusal", nudge_count=1, session_refusal_count=1,
        )
        sidecar = _tmp_telemetry_home / ".refusal_telemetry.json"
        assert sidecar.exists()
        data = json.loads(sidecar.read_text(encoding="utf-8"))
        assert data["version"] == 1
        assert len(data["events"]) == 1


# ── Best-effort / robustness ────────────────────────────────────────────────────


class TestBestEffort:
    def test_corrupt_sidecar_returns_empty(self, _tmp_telemetry_home):
        (_tmp_telemetry_home / ".refusal_telemetry.json").write_text("not json {{{", encoding="utf-8")
        assert refusal_telemetry.load_events() == []

    def test_record_does_not_raise_on_io_error(self, _tmp_telemetry_home):
        with patch("agent.refusal_telemetry._save_events", side_effect=OSError("disk full")):
            refusal_telemetry.record_nudge(
                session_id="s1", nudge_tier="advisory",
                refusal_category="over_refusal", nudge_count=1, session_refusal_count=1,
            )

    def test_record_transition_does_not_raise_on_io_error(self, _tmp_telemetry_home):
        with patch("agent.refusal_telemetry._save_events", side_effect=OSError("disk full")):
            refusal_telemetry.record_transition(
                session_id="s1", nudge_tier="advisory",
                category_before="over_refusal", category_after="",
                recovered=True, took_action=True,
            )

    def test_max_events_cap(self, _tmp_telemetry_home):
        original = refusal_telemetry._MAX_EVENTS
        refusal_telemetry._MAX_EVENTS = 5
        try:
            for i in range(10):
                refusal_telemetry.record_nudge(
                    session_id=f"s{i}", nudge_tier="advisory",
                    refusal_category="over_refusal", nudge_count=1, session_refusal_count=1,
                )
            events = refusal_telemetry.load_events()
            assert len(events) == 5
            assert events[0]["session_id"] == "s5"
            assert events[-1]["session_id"] == "s9"
        finally:
            refusal_telemetry._MAX_EVENTS = original

# ── #2168 — recovery_rate metric ────────────────────────────────────────────


class TestRecoveryRate:
    def test_no_transitions_returns_none(self, _tmp_telemetry_home):
        assert refusal_telemetry.recovery_rate() is None

    def test_half_recovered(self, _tmp_telemetry_home):
        refusal_telemetry.record_transition(
            session_id="s1", nudge_tier="advisory",
            category_before="over_refusal", category_after="",
            recovered=True, took_action=True,
        )
        refusal_telemetry.record_transition(
            session_id="s2", nudge_tier="advisory",
            category_before="over_refusal", category_after="over_refusal",
            recovered=False, took_action=False,
        )
        rate = refusal_telemetry.recovery_rate()
        assert rate is not None
        assert rate["recovery_rate"] == 0.5
        assert rate["recovered"] == 1.0
        assert rate["unrecovered"] == 1.0

    def test_took_action_counts_as_recovered(self, _tmp_telemetry_home):
        refusal_telemetry.record_transition(
            session_id="s1", nudge_tier="advisory",
            category_before="over_refusal", category_after="over_refusal",
            recovered=False, took_action=True,
        )
        rate = refusal_telemetry.recovery_rate()
        assert rate is not None and rate["recovery_rate"] == 1.0

    def test_corrupt_sidecar_returns_none(self, _tmp_telemetry_home):
        (_tmp_telemetry_home / ".refusal_telemetry.json").write_text(
            "CORRUPT{", encoding="utf-8"
        )
        assert refusal_telemetry.recovery_rate() is None
