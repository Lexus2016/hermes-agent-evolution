"""Tests for Issue #3106: Structured Refusal & Access-Denied Recovery Policy.

Verifies:
1. Category classification across safety_refusal, rate_limit, unsupported_parameter,
   permission_boundary, true_capability_gap, and over_refusal.
2. 2-stage recovery retry ladder (advisory -> directive).
3. Telemetry tracking (record_nudge, record_transition, record_transition_if_pending).
4. Turn finalization and synthetic scaffolding cleanup.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch
import pytest

from agent.loop_guard import (
    detect_refusal_category,
    maybe_refusal_nudge,
    _REFUSAL_CATEGORIES,
)
from agent import refusal_telemetry
from agent.turn_finalizer import _drop_verification_continuation_scaffolding


class TestRefusalTaxonomyClassification:
    """Test classification across all structured refusal categories."""

    def test_safety_refusal_detection(self):
        text = "I cannot fulfill this request as it violates safety guidelines and policy."
        cat = detect_refusal_category(text)
        assert cat == "safety_refusal"

    def test_rate_limit_detection(self):
        text = "I'm unable to proceed because we hit a 429 rate limit on the endpoint."
        cat = detect_refusal_category(text)
        assert cat == "rate_limit"

    def test_unsupported_parameter_detection(self):
        text = "I cannot run this tool because of an invalid parameter schema in the call."
        cat = detect_refusal_category(text)
        assert cat == "unsupported_parameter"

    def test_permission_boundary_detection(self):
        text = "I don't have access to /etc/shadow due to permission denied (403 forbidden)."
        cat = detect_refusal_category(text)
        assert cat == "permission_boundary"

    def test_true_capability_gap_detection(self):
        text = "I don't have a tool to search the live web or browse URLs."
        cat = detect_refusal_category(text)
        assert cat == "true_capability_gap"

    def test_over_refusal_detection(self):
        text = "I'm unable to do that for you right now."
        cat = detect_refusal_category(text)
        assert cat == "over_refusal"

    def test_false_positive_ignored(self):
        text = "I can't imagine how fast this algorithm runs on large datasets!"
        cat = detect_refusal_category(text)
        assert cat == ""


class TestTwoStageRefusalNudgeLadder:
    """Test 1st stage advisory nudge vs 2nd stage directive nudge."""

    def test_stage_1_advisory_nudge(self):
        messages = [
            {"role": "user", "content": "Fetch the report"},
            {"role": "assistant", "content": "I don't have permission to access that API."},
        ]
        nudge = maybe_refusal_nudge(messages, already_nudged=False, nudge_count=1)
        assert nudge is not None
        assert "Refusal detected (permission_boundary)" in nudge
        assert "Do not simply repeat the refusal" in nudge

    def test_stage_2_directive_nudge(self):
        messages = [
            {"role": "user", "content": "Fetch the report"},
            {"role": "assistant", "content": "I don't have permission to access that API."},
        ]
        nudge = maybe_refusal_nudge(messages, already_nudged=True, nudge_count=2)
        assert nudge is not None
        assert "Second refusal detected (permission_boundary)" in nudge
        assert "alternative approach" in nudge
        assert "missing credentials, exact permissions" in nudge

    def test_no_nudge_for_non_refusal_text(self):
        messages = [
            {"role": "user", "content": "What is 2+2?"},
            {"role": "assistant", "content": "The answer is 4."},
        ]
        nudge = maybe_refusal_nudge(messages)
        assert nudge is None


class TestRefusalTelemetryIntegration:
    """Test refusal telemetry recording and transition tracking."""

    def test_record_nudge_and_transition(self, tmp_path, monkeypatch):
        monkeypatch.setattr("agent.refusal_telemetry.get_hermes_home", lambda: tmp_path)
        agent = SimpleNamespace(
            session_id="test-session-3106",
            _session_refusal_count=1,
            _pending_nudge_telemetry=None,
        )

        # Stage 1: Record nudge
        refusal_telemetry.record_nudge_and_set_pending(
            agent,
            refusal_category="permission_boundary",
            nudge_tier="advisory",
            nudge_count=1,
        )
        assert agent._pending_nudge_telemetry == {
            "tier": "advisory",
            "category": "permission_boundary",
        }

        # Stage 2: Successful action recovery (tool call)
        refusal_telemetry.record_transition_if_pending(
            agent,
            category_after="",
            took_action=True,
        )
        assert agent._pending_nudge_telemetry is None

        events = refusal_telemetry.load_events()
        assert len(events) == 2
        assert events[0]["type"] == "nudge"
        assert events[0]["refusal_category"] == "permission_boundary"
        assert events[1]["type"] == "transition"
        assert events[1]["category_before"] == "permission_boundary"
        assert events[1]["recovered"] is True
        assert events[1]["took_action"] is True


class TestSyntheticScaffoldingCleanup:
    """Test that _refusal_recovery_synthetic flags are cleaned up on turn finalization."""

    def test_drop_refusal_recovery_synthetic_messages(self):
        messages = [
            {"role": "user", "content": "Deploy the app"},
            {
                "role": "assistant",
                "content": "I cannot deploy without credentials.",
                "_refusal_recovery_synthetic": True,
            },
            {
                "role": "user",
                "content": "[loop-guard] Refusal detected...",
                "_refusal_recovery_synthetic": True,
            },
            {"role": "assistant", "content": "I have created the deployment script instead."},
        ]

        _drop_verification_continuation_scaffolding(messages)

        assert len(messages) == 2
        assert messages[0]["content"] == "Deploy the app"
        assert messages[1]["content"] == "I have created the deployment script instead."
