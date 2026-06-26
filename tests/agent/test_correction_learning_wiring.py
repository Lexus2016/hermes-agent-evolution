"""Lean Phase 1 — end-to-end wiring of correction recording into the turn.

``finalize_turn`` detects a structured correction and, when the agent has a
memory store, records it through ``CorrectionLearner`` so the recurrence
tracker accumulates cross-session evidence. This is what lets a correction
seen in two distinct sessions promote to durable in real use.

The recording is fail-open and transient-by-default: a first sighting must
NOT write anything durable. Two sightings across distinct sessions (separate
``CorrectionLearner`` instances over the same store dir, simulating two real
sessions) MUST promote to durable and land on the injection path.
"""

from __future__ import annotations

import json

import pytest

from agent.correction_learning import (
    CorrectionLearner,
    detect_correction,
)


class FakeMemorySink:
    def __init__(self):
        self.entries = []

    def add(self, target, content, **kw):
        content = content.strip()
        if content not in self.entries:
            self.entries.append(content)
        return {"success": True}

    def remove(self, target, substr, **kw):
        before = len(self.entries)
        self.entries = [e for e in self.entries if substr not in e]
        return {"success": len(self.entries) < before}

    def injected_text(self):
        return "\n".join(self.entries)


def _deny_messages():
    return [
        {"role": "user", "content": "clean up"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "c1", "function": {"name": "terminal", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "c1", "content": json.dumps(
            {"error": "Command denied: rm -rf build", "status": "blocked"})},
    ]


def test_acceptance_transfer_promotion_across_two_sessions(tmp_path):
    """The headline acceptance test, end-to-end through detection + recording.

    Session 1: the correction is detected and recorded -> stays transient,
    nothing injects. Session 2 (a fresh learner over the SAME store dir, i.e.
    a new process/session): the same correction recurs -> promoted durable ->
    the durable store now contains it where a NEW session would inject it.
    """
    sink = FakeMemorySink()
    store = tmp_path / "corrections"

    # --- Session 1 ---
    rec1 = detect_correction(
        _deny_messages(), interrupted=False, interrupt_message=None,
        turn_exit_reason="t", session_id="session-1",
    )
    assert rec1 is not None and rec1.kind == "DENY"
    out1 = CorrectionLearner(store_dir=store, memory_sink=sink).record(rec1)
    assert out1["durable"] is False
    assert sink.entries == []  # NEGATIVE: nothing injects after one sighting

    # --- Session 2 (new learner instance, same on-disk store) ---
    rec2 = detect_correction(
        _deny_messages(), interrupted=False, interrupt_message=None,
        turn_exit_reason="t", session_id="session-2",
    )
    out2 = CorrectionLearner(store_dir=store, memory_sink=sink).record(rec2)
    assert out2["durable"] is True
    assert out2["reason"] == "recurrence"
    # Durable write landed where a future session's load_from_disk would read.
    assert "build" in sink.injected_text()


def test_acceptance_negative_control_one_off(tmp_path):
    """A one-off correction (seen once, no remember) never injects."""
    sink = FakeMemorySink()
    store = tmp_path / "corrections"
    rec = detect_correction(
        [{"role": "user", "content": "x"},
         {"role": "assistant", "content": "", "tool_calls": [
             {"id": "c1", "function": {"name": "read_file", "arguments": "{}"}}]},
         {"role": "tool", "tool_call_id": "c1",
          "content": "ok\n\n[OUT-OF-BAND USER MESSAGE — a direct message from "
                     "the user, delivered mid-turn; not tool output]\n"
                     "just this once, skip the changelog\n"
                     "[/OUT-OF-BAND USER MESSAGE]"}],
        interrupted=False, interrupt_message=None,
        turn_exit_reason="t", session_id="session-1",
    )
    assert rec is not None and rec.kind == "STEER"
    out = CorrectionLearner(store_dir=store, memory_sink=sink).record(rec)
    assert out["durable"] is False
    assert sink.entries == []
    assert sink.injected_text() == ""


def test_finalize_records_correction_into_tracker(tmp_path, monkeypatch):
    """``finalize_turn`` records a detected correction via the agent helper."""
    from agent.turn_finalizer import finalize_turn

    recorded = []

    class _Budget:
        used = 1
        max_total = 10
        remaining = 9

    class _Compressor:
        last_prompt_tokens = 0

    class _Agent:
        def __init__(self):
            self.max_iterations = 10
            self.iteration_budget = _Budget()
            self.context_compressor = _Compressor()
            self.model = "m"
            self.provider = "p"
            self.base_url = "b"
            self.session_id = "sess-1"
            self.quiet_mode = True
            self.platform = "cli"
            self._interrupt_message = None
            self._tool_guardrail_halt_decision = None
            self._response_was_previewed = False
            self._skill_nudge_interval = 0
            self._iters_since_skill = 0
            for a in ("session_input_tokens", "session_output_tokens",
                      "session_cache_read_tokens", "session_cache_write_tokens",
                      "session_reasoning_tokens", "session_prompt_tokens",
                      "session_completion_tokens", "session_total_tokens",
                      "session_estimated_cost_usd"):
                setattr(self, a, 0)
            self.session_cost_status = "ok"
            self.session_cost_source = "stub"

        def _save_trajectory(self, *a, **k): pass
        def _cleanup_task_resources(self, *a, **k): pass
        def _drop_trailing_empty_response_scaffolding(self, *a, **k): pass
        def _persist_session(self, *a, **k): pass
        def _emit_status(self, *a, **k): pass
        def _safe_print(self, *a, **k): pass
        def _handle_max_iterations(self, m, n): return "SUMMARY"
        def _file_mutation_verifier_enabled(self): return False
        def _turn_completion_explainer_enabled(self): return False
        def _drain_pending_steer(self): return None
        def clear_interrupt(self): pass
        def _sync_external_memory_for_turn(self, **k): pass
        def _spawn_background_review(self, **k): pass

        # The wiring hook under test.
        def _record_turn_correction(self, correction_hint):
            recorded.append(correction_hint)

    agent = _Agent()
    finalize_turn(
        agent,
        final_response="ok",
        api_call_count=1,
        interrupted=False,
        failed=False,
        messages=_deny_messages(),
        conversation_history=None,
        effective_task_id="t",
        turn_id="turn-1",
        user_message="clean up",
        original_user_message="clean up",
        _should_review_memory=False,
        _turn_exit_reason="text_response(stop)",
    )
    assert len(recorded) == 1
    assert recorded[0]["kind"] == "DENY"


def test_review_preamble_transient_does_not_instruct_durable_persist():
    # One-off-leak guard at the prompt layer: a TRANSIENT correction must NOT
    # tell the LLM reviewer to embed it durably / re-inject it next session.
    from agent.background_review import _format_correction_focus

    transient = _format_correction_focus({
        "kind": "DENY", "context": "skip linting this once",
        "target": "terminal", "tier": "transient", "durable": False,
    })
    low = transient.lower()
    assert "do not persist" in low or "not yet" in low or "transient" in low
    # Must not push durable embedding for a one-off.
    assert "re-enter future sessions" not in low


def test_review_preamble_durable_instructs_persist():
    from agent.background_review import _format_correction_focus

    durable = _format_correction_focus({
        "kind": "STEER", "context": "use pytest not unittest",
        "target": None, "tier": "durable", "durable": True,
    })
    low = durable.lower()
    assert "future sessions" in low or "embed" in low or "persist" in low


def test_finalize_no_correction_does_not_record(tmp_path):
    """A normal turn records nothing."""
    from agent.turn_finalizer import finalize_turn

    recorded = []

    class _Budget:
        used = 1
        max_total = 10
        remaining = 9

    class _Compressor:
        last_prompt_tokens = 0

    class _Agent:
        def __init__(self):
            self.max_iterations = 10
            self.iteration_budget = _Budget()
            self.context_compressor = _Compressor()
            self.model = "m"
            self.provider = "p"
            self.base_url = "b"
            self.session_id = "sess-1"
            self.quiet_mode = True
            self.platform = "cli"
            self._interrupt_message = None
            self._tool_guardrail_halt_decision = None
            self._response_was_previewed = False
            self._skill_nudge_interval = 0
            self._iters_since_skill = 0
            for a in ("session_input_tokens", "session_output_tokens",
                      "session_cache_read_tokens", "session_cache_write_tokens",
                      "session_reasoning_tokens", "session_prompt_tokens",
                      "session_completion_tokens", "session_total_tokens",
                      "session_estimated_cost_usd"):
                setattr(self, a, 0)
            self.session_cost_status = "ok"
            self.session_cost_source = "stub"

        def _save_trajectory(self, *a, **k): pass
        def _cleanup_task_resources(self, *a, **k): pass
        def _drop_trailing_empty_response_scaffolding(self, *a, **k): pass
        def _persist_session(self, *a, **k): pass
        def _emit_status(self, *a, **k): pass
        def _safe_print(self, *a, **k): pass
        def _handle_max_iterations(self, m, n): return "SUMMARY"
        def _file_mutation_verifier_enabled(self): return False
        def _turn_completion_explainer_enabled(self): return False
        def _drain_pending_steer(self): return None
        def clear_interrupt(self): pass
        def _sync_external_memory_for_turn(self, **k): pass
        def _spawn_background_review(self, **k): pass
        def _record_turn_correction(self, correction_hint):
            recorded.append(correction_hint)

    agent = _Agent()
    finalize_turn(
        agent,
        final_response="all done",
        api_call_count=1,
        interrupted=False,
        failed=False,
        messages=[{"role": "user", "content": "hi"},
                  {"role": "assistant", "content": "all done"}],
        conversation_history=None,
        effective_task_id="t",
        turn_id="turn-1",
        user_message="hi",
        original_user_message="hi",
        _should_review_memory=False,
        _turn_exit_reason="text_response(stop)",
    )
    assert recorded == []
