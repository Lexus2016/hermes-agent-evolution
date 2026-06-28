"""Lean Phase 1 — correction-driven review in ``finalize_turn``.

The legacy gate only spawned the background review when
``final_response and not interrupted and (review_memory or review_skills)``.
That SKIPPED the loudest corrections: an interrupted or denied turn never
reached the learner.

Phase 1 (current contract, routed through ``agent/correction_review.py``):

* A structured correction (INTERRUPT / DENY / STEER) is DETECTED + RECORDED
  deterministically on EVERY turn — even interrupted/denied — via the
  ``_record_turn_correction`` hook (the CorrectionLearner). This always runs.
* The expensive LLM review fork is spawned ONLY when a nudge counter fired
  (the legacy healthy-completion path) OR the correction was promoted to
  DURABLE. A pure-transient correction with no nudge is recorded but does NOT
  spawn the fork (it would be write-blocked anyway — wasted aux-model spend).
* X1 (universal): whenever the fork DOES spawn while an unpromoted correction
  is present, it runs with ``block_durable_writes=True`` so the deterministic
  recurrence guard stays the single durable gate.
* Non-correction normal/nudge turns keep their exact prior behavior.
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
        # Mirror production AIAgent.clear_interrupt (run_agent.py): null the
        # interrupt message + request flag. A no-op stub here would MASK the
        # capture-before-clear bug — finalize_turn calls clear_interrupt()
        # ~46 lines BEFORE the correction detector reads _interrupt_message,
        # so a stub that never nulls it lets the dead INTERRUPT branch "pass".
        self._interrupt_message = None
        self._interrupt_requested = False

    def _sync_external_memory_for_turn(self, **k):
        pass

    def _spawn_background_review(self, *, messages_snapshot, review_memory,
                                review_skills, correction_hint=None,
                                block_durable_writes=False):
        self.spawned.append({
            "review_memory": review_memory,
            "review_skills": review_skills,
            "correction_hint": correction_hint,
            "block_durable_writes": block_durable_writes,
        })


def _transient_recorder(agent):
    """Attach a recorder that captures hints and reports transient."""
    recorded = []

    def _rec(hint):
        recorded.append(hint)
        return {"tier": "transient", "durable": False}

    agent._record_turn_correction = _rec
    return recorded


def _durable_recorder(agent):
    """Attach a recorder that captures hints and promotes to durable."""
    recorded = []

    def _rec(hint):
        recorded.append(hint)
        return {"tier": "durable", "durable": True}

    agent._record_turn_correction = _rec
    return recorded


def _normal_messages():
    return [
        {"role": "user", "content": "do a thing"},
        {"role": "assistant", "content": "done"},
    ]


def _deny_messages():
    # A GENUINE user denial: the approval flow stamps ``user_denied: True`` into
    # the tool result (see tools/approval.py + tools/terminal_tool.py). The
    # detector keys on THAT marker, not the bare ``status: "blocked"`` that
    # automatic safety blocks also produce.
    return [
        {"role": "user", "content": "clean up"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "c1", "function": {"name": "terminal", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "c1", "content": json.dumps(
            {"error": "Command denied: rm -rf build", "status": "blocked",
             "user_denied": True})},
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
# Corrections are DETECTED + RECORDED deterministically (always). The fork is
# the EXPENSIVE step and is reserved for a nudge or a DURABLE promotion.
# ---------------------------------------------------------------------------


def test_denied_correction_recorded_but_no_fork_without_nudge():
    # A genuine denial with no nudge and no promotion: the deterministic
    # recorder captures it, but the LLM fork is NOT spawned (it would be
    # write-blocked anyway — wasted aux-model spend). This is the DEFECT 4
    # optimization AND proves the loud denial is still captured.
    agent = _StubAgent()
    recorded = _transient_recorder(agent)
    _run(agent, messages=_deny_messages(), interrupted=False,
         should_review_memory=False)
    assert len(recorded) == 1
    assert recorded[0]["kind"] == "DENY"
    assert agent.spawned == []  # no fork for a pure-transient correction


def test_interrupted_correction_recorded_but_no_fork_without_nudge():
    # The loudest correction (user interrupted + redirected) is captured even
    # though the legacy ``not interrupted`` gate dropped it — recorded
    # deterministically, no fork without a nudge.
    agent = _StubAgent()
    recorded = _transient_recorder(agent)
    _run(agent, messages=_normal_messages(), interrupted=True,
         final_response="", interrupt_message="no, use TypeScript instead",
         turn_exit_reason="interrupted_by_user")
    assert len(recorded) == 1
    assert recorded[0]["kind"] == "INTERRUPT"
    assert agent.spawned == []


def test_denied_correction_with_nudge_spawns_fork_with_hint():
    # When a nudge co-occurs, the fork spawns and carries the correction hint.
    agent = _StubAgent()
    _transient_recorder(agent)
    _run(agent, messages=_deny_messages(), interrupted=False,
         should_review_memory=True)
    assert len(agent.spawned) == 1
    hint = agent.spawned[0]["correction_hint"]
    assert hint is not None
    assert hint["kind"] == "DENY"


def test_durable_correction_spawns_fork_with_hint():
    # A promoted (durable) correction spawns the fork even with no nudge.
    agent = _StubAgent()
    _durable_recorder(agent)
    _run(agent, messages=_normal_messages(), interrupted=True,
         final_response="", interrupt_message="no, use TypeScript instead",
         turn_exit_reason="interrupted_by_user")
    assert len(agent.spawned) == 1
    hint = agent.spawned[0]["correction_hint"]
    assert hint["kind"] == "INTERRUPT"


def test_interrupt_message_captured_before_clear_through_real_ordering():
    # DEFECT 1 regression — capture-before-clear, proven through the REAL
    # finalize_turn ordering with the production-mirroring stub.
    #
    # finalize_turn calls agent.clear_interrupt() (which nulls _interrupt_message,
    # exactly as production AIAgent.clear_interrupt does) ~46 lines BEFORE the
    # correction detector reads the interrupt message. If finalize_turn does not
    # capture the message into a LOCAL before that clear, the INTERRUPT branch is
    # dead on the default runtime. This test pins both halves of the fix:
    #   * clear_interrupt actually ran  -> the live attribute is None afterwards
    #   * the INTERRUPT correction was STILL detected, carrying the exact message
    #     -> the captured local (not the already-nulled attribute) fed detection.
    agent = _StubAgent()
    recorded = _transient_recorder(agent)
    _run(agent, messages=_normal_messages(), interrupted=True,
         final_response="", interrupt_message="stop, use the staging DB",
         turn_exit_reason="interrupted_by_user")
    # Production-mirroring stub nulled the live attribute -> proves clear ran.
    assert agent._interrupt_message is None
    # Yet the correction was detected with the real redirect text -> proves the
    # message was captured BEFORE the clear and threaded into detection.
    assert len(recorded) == 1
    assert recorded[0]["kind"] == "INTERRUPT"
    assert recorded[0]["context"] == "stop, use the staging DB"


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
    # The one-off-leak guard: the recorder's tier decision is threaded into the
    # hint so the review prompt can stay transient-aware. A co-occurring nudge
    # makes the fork spawn so the hint is observable; the recorder reports
    # transient -> the hint must say transient.
    agent = _StubAgent()
    _transient_recorder(agent)
    _run(agent, messages=_deny_messages(), interrupted=False,
         should_review_memory=True)
    assert len(agent.spawned) == 1
    hint = agent.spawned[0]["correction_hint"]
    assert hint["tier"] == "transient"
    assert hint["durable"] is False


def test_correction_hint_tier_durable_when_recorder_promotes():
    agent = _StubAgent()
    _durable_recorder(agent)
    _run(agent, messages=_deny_messages(), interrupted=False)
    hint = agent.spawned[0]["correction_hint"]
    assert hint["tier"] == "durable"
    assert hint["durable"] is True


# ---------------------------------------------------------------------------
# X1 ENFORCEMENT + no-waste spawn rule.
#   * A transient correction NEVER persists durable via the fork: when the fork
#     spawns at all (because a nudge co-occurred) it is handed
#     ``block_durable_writes=True`` (universal — DEFECT 3).
#   * A pure-transient correction with no nudge does NOT spawn the fork at all
#     (DEFECT 4 — no wasted aux-model call).
#   * A durable correction keeps write capability.
# ---------------------------------------------------------------------------


def test_transient_correction_with_nudge_blocks_durable_writes():
    # DEFECT 3 (universal X1): a transient correction co-occurring with a nudge
    # MUST hand the spawned fork block_durable_writes=True. The nudge's own
    # durable write is deferred to the next nudge interval so a one-off can
    # never ride a nudge into a durable write.
    agent = _StubAgent()
    _transient_recorder(agent)
    _run(agent, messages=_deny_messages(), interrupted=False,
         should_review_memory=True)
    assert len(agent.spawned) == 1
    assert agent.spawned[0]["block_durable_writes"] is True


def test_pure_transient_correction_no_nudge_does_not_spawn_fork():
    # DEFECT 4: pure-transient correction, no nudge -> NO fork spawned at all.
    # The deterministic CorrectionLearner already recorded it; the fork would be
    # write-blocked, so spawning it would burn an aux-model call for nothing.
    agent = _StubAgent()
    _transient_recorder(agent)
    _run(agent, messages=_deny_messages(), interrupted=False,
         should_review_memory=False)
    assert agent.spawned == []


def test_durable_correction_does_not_block_writes():
    # A promotable (recurred / explicit-remember) correction is confirmed; its
    # durable write already happened via the deterministic path. The fork keeps
    # write capability (it may embed the confirmed preference into a skill).
    agent = _StubAgent()
    _durable_recorder(agent)
    _run(agent, messages=_deny_messages(), interrupted=False,
         should_review_memory=False)
    assert len(agent.spawned) == 1
    assert agent.spawned[0]["block_durable_writes"] is False


def test_nudge_review_unchanged_does_not_block_writes():
    # Pre-existing NUDGE-driven (non-correction) review behavior is out of
    # scope: a healthy nudge review keeps full durable-write capability.
    agent = _StubAgent()
    _run(agent, messages=_normal_messages(), interrupted=False,
         should_review_memory=True)
    assert len(agent.spawned) == 1
    assert agent.spawned[0]["correction_hint"] is None
    assert agent.spawned[0]["block_durable_writes"] is False
