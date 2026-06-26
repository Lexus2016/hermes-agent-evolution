"""Lean Phase 1 — the ``not interrupted`` guard fix in ``finalize_turn``.

Today ``finalize_turn`` only spawns the background review when
``final_response and not interrupted and (review_memory or review_skills)``
(agent/turn_finalizer.py:427). That SKIPS the loudest corrections: an
interrupted or denied turn never reaches the reviewer.

Phase 1: a turn that IS a structured correction (INTERRUPT / DENY / STEER)
still triggers the review, even when ``interrupted`` is true or no nudge
counter fired, and the spawn is passed a hint of the correction kind so the
reviewer captures THAT. Non-correction interrupted turns and normal turns
keep their exact prior behavior.
"""

from __future__ import annotations

import json

import pytest

from agent.turn_finalizer import finalize_turn


class _StubBudget:
    used = 1
    max_total = 10
    remaining = 9


class _StubCompressor:
    last_prompt_tokens = 0


class _StubAgent:
    def __init__(self):
        self.max_iterations = 10
        self.iteration_budget = _StubBudget()
        self.context_compressor = _StubCompressor()
        self.model = "stub/model"
        self.provider = "stub"
        self.base_url = "http://stub"
        self.session_id = "sess-1"
        self.quiet_mode = True
        self.platform = "cli"
        self._interrupt_requested = False
        self._interrupt_message = None
        self._tool_guardrail_halt_decision = None
        self._response_was_previewed = False
        self._skill_nudge_interval = 0
        self._iters_since_skill = 0
        self.spawned = []  # records (review_memory, review_skills, correction_hint)
        for attr in (
            "session_input_tokens", "session_output_tokens",
            "session_cache_read_tokens", "session_cache_write_tokens",
            "session_reasoning_tokens", "session_prompt_tokens",
            "session_completion_tokens", "session_total_tokens",
            "session_estimated_cost_usd",
        ):
            setattr(self, attr, 0)
        self.session_cost_status = "ok"
        self.session_cost_source = "stub"

    # cleanup surfaces — all no-ops here
    def _save_trajectory(self, *a, **k):
        pass

    def _cleanup_task_resources(self, *a, **k):
        pass

    def _drop_trailing_empty_response_scaffolding(self, *a, **k):
        pass

    def _persist_session(self, *a, **k):
        pass

    def _emit_status(self, *a, **k):
        pass

    def _safe_print(self, *a, **k):
        pass

    def _handle_max_iterations(self, messages, n):
        return "SUMMARY"

    def _file_mutation_verifier_enabled(self):
        return False

    def _turn_completion_explainer_enabled(self):
        return False

    def _drain_pending_steer(self):
        return None

    def clear_interrupt(self):
        pass

    def _sync_external_memory_for_turn(self, **k):
        pass

    def _spawn_background_review(self, *, messages_snapshot, review_memory,
                                review_skills, correction_hint=None):
        self.spawned.append({
            "review_memory": review_memory,
            "review_skills": review_skills,
            "correction_hint": correction_hint,
        })


def _normal_messages():
    return [
        {"role": "user", "content": "do a thing"},
        {"role": "assistant", "content": "done"},
    ]


def _deny_messages():
    return [
        {"role": "user", "content": "clean up"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "c1", "function": {"name": "terminal", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "c1", "content": json.dumps(
            {"error": "Command denied: rm -rf build", "status": "blocked"})},
    ]


def _run(agent, *, messages, interrupted, final_response="ok",
         should_review_memory=False, interrupt_message=None,
         turn_exit_reason="text_response(stop)"):
    agent._interrupt_message = interrupt_message
    return finalize_turn(
        agent,
        final_response=final_response,
        api_call_count=1,
        interrupted=interrupted,
        failed=False,
        messages=messages,
        conversation_history=None,
        effective_task_id="task-1",
        turn_id="turn-1",
        user_message="do a thing",
        original_user_message="do a thing",
        _should_review_memory=should_review_memory,
        _turn_exit_reason=turn_exit_reason,
    )


# ---------------------------------------------------------------------------
# Corrections now trigger the review (the fix).
# ---------------------------------------------------------------------------


def test_denied_turn_triggers_review_with_hint():
    agent = _StubAgent()
    _run(agent, messages=_deny_messages(), interrupted=False,
         should_review_memory=False)
    assert len(agent.spawned) == 1
    hint = agent.spawned[0]["correction_hint"]
    assert hint is not None
    assert hint["kind"] == "DENY"


def test_interrupted_correction_triggers_review_with_hint():
    agent = _StubAgent()
    _run(agent, messages=_normal_messages(), interrupted=True,
         final_response="", interrupt_message="no, use TypeScript instead",
         turn_exit_reason="interrupted_by_user")
    assert len(agent.spawned) == 1
    hint = agent.spawned[0]["correction_hint"]
    assert hint["kind"] == "INTERRUPT"


# ---------------------------------------------------------------------------
# REGRESSION — non-corrections behave exactly as before.
# ---------------------------------------------------------------------------


def test_normal_turn_no_nudge_does_not_review():
    # No correction, no nudge counter -> no spawn (unchanged behavior).
    agent = _StubAgent()
    _run(agent, messages=_normal_messages(), interrupted=False,
         should_review_memory=False)
    assert agent.spawned == []


def test_normal_turn_with_nudge_still_reviews():
    # The existing counter-driven path still fires for healthy turns.
    agent = _StubAgent()
    _run(agent, messages=_normal_messages(), interrupted=False,
         should_review_memory=True)
    assert len(agent.spawned) == 1
    assert agent.spawned[0]["review_memory"] is True
    # Healthy turn -> no correction hint.
    assert agent.spawned[0]["correction_hint"] is None


def test_plain_interrupt_without_redirect_does_not_review():
    # User hit stop, gave no redirect, no nudge -> NOT a learnable correction.
    # Prior behavior (skip) preserved.
    agent = _StubAgent()
    _run(agent, messages=_normal_messages(), interrupted=True,
         final_response="", interrupt_message=None,
         turn_exit_reason="interrupted_by_user")
    assert agent.spawned == []


def test_correction_hint_carries_tier_from_recorder():
    # The one-off-leak guard: the recorder's tier decision is threaded into
    # the hint so the review prompt can stay transient-aware (it must not push
    # the LLM to durably persist a first-sighting one-off). Here the recorder
    # reports transient -> the hint must say transient.
    agent = _StubAgent()

    def _recorder(hint):
        return {"tier": "transient", "durable": False}

    agent._record_turn_correction = _recorder
    _run(agent, messages=_deny_messages(), interrupted=False)
    assert len(agent.spawned) == 1
    hint = agent.spawned[0]["correction_hint"]
    assert hint["tier"] == "transient"
    assert hint["durable"] is False


def test_correction_hint_tier_durable_when_recorder_promotes():
    agent = _StubAgent()

    def _recorder(hint):
        return {"tier": "durable", "durable": True}

    agent._record_turn_correction = _recorder
    _run(agent, messages=_deny_messages(), interrupted=False)
    hint = agent.spawned[0]["correction_hint"]
    assert hint["tier"] == "durable"
    assert hint["durable"] is True
