# -*- coding: utf-8 -*-
"""Unit tests for multi-agent cooperation/containment rules + escalation (#2527)."""

from evolution.lib.agent_cooperation import (
    COOPERATION_RULES,
    ConflictSignal,
    EscalationMessage,
    build_escalation_message,
    cooperation_directives_for_goal,
    detect_conflict_signal,
)


class TestCooperationDirectives:
    def test_no_directives_for_lone_agent(self):
        assert cooperation_directives_for_goal("do a task", peer_count=0) == ""

    def test_directives_for_concurrent_peers(self):
        directives = cooperation_directives_for_goal("do a task", peer_count=3)
        assert (
            "COOPERATION RULES (3 concurrent peer agent(s) share this infrastructure)"
            in directives
        )
        assert "do not retaliate" in directives.lower()
        assert "escalate" in directives.lower()

    def test_all_rules_present(self):
        directives = cooperation_directives_for_goal("task", peer_count=2)
        for rule in COOPERATION_RULES:
            assert rule in directives


class TestDetectConflictSignal:
    def test_no_conflict_returns_none(self):
        assert detect_conflict_signal("agent-1", "task completed successfully") is None

    def test_blocked_signal_detected(self):
        signal = detect_conflict_signal(
            "agent-1", "my work was blocked by another agent"
        )
        assert signal is not None
        assert signal.conflict_type == "blocked"
        assert signal.agent_id == "agent-1"

    def test_locked_out_signal_detected(self):
        signal = detect_conflict_signal("agent-2", "I was locked out of the server")
        assert signal is not None
        assert signal.conflict_type == "locked_out"

    def test_impersonation_signal_detected(self):
        signal = detect_conflict_signal("agent-3", "another agent is impersonating me")
        assert signal is not None
        assert signal.conflict_type == "impersonated"

    def test_retaliation_signal_detected(self):
        signal = detect_conflict_signal(
            "agent-4", "I deployed self-replicating code in retaliation"
        )
        assert signal is not None
        assert signal.conflict_type == "retaliated"


class TestEscalationMessage:
    def test_build_escalation_message(self):
        signal = ConflictSignal(
            agent_id="agent-1",
            conflict_type="blocked",
            description="my work was blocked by another agent",
        )
        esc = build_escalation_message(signal)
        assert isinstance(esc, EscalationMessage)
        assert "INTER-AGENT CONFLICT ESCALATION" in esc.message
        assert "agent-1" in esc.message
        assert "blocked" in esc.message
        assert esc.action_requested == "review and coordinate"

    def test_escalation_serialization(self):
        signal = ConflictSignal(
            agent_id="a", conflict_type="halted", description="d", peer_agent_id="b"
        )
        esc = build_escalation_message(signal)
        d = esc.to_dict()
        assert d["agent_id"] == "a"
        assert d["conflict_type"] == "halted"
        assert d["peer_agent_id"] == "b"
        assert d["action_requested"] == "review and coordinate"
