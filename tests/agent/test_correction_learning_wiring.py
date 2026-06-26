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
    # Genuine USER denial — carries the ``user_denied`` marker the detector
    # keys on (an automatic safety block sets ``status: "blocked"`` without it).
    return [
        {"role": "user", "content": "clean up"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "c1", "function": {"name": "terminal", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "c1", "content": json.dumps(
            {"error": "Command denied: rm -rf build", "status": "blocked",
             "user_denied": True})},
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
    """A one-off correction (seen once, no remember) never injects DETERMINISTICALLY.

    Scope note: this exercises ONLY the deterministic ``CorrectionLearner`` —
    which was never the leak path. The actual one-off leak risk is the LLM
    review fork; that is gated by ``test_transient_correction_fork_cannot_write_durable``
    below (the fork's runtime tool whitelist strips the durable writers), not by
    this test.
    """
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
    # DEFENSE-IN-DEPTH at the prompt layer (NOT the enforcement): a TRANSIENT
    # correction's preamble must not tell the LLM reviewer to embed it durably.
    # The real guard is the tool whitelist (see the enforcement test below);
    # this preamble is belt-and-suspenders, not the gate.
    from agent.background_review import _format_correction_focus

    transient = _format_correction_focus({
        "kind": "DENY", "context": "skip linting this once",
        "target": "terminal", "tier": "transient", "durable": False,
    })
    low = transient.lower()
    assert "do not persist" in low or "not yet" in low or "transient" in low
    # Must not push durable embedding for a one-off.
    assert "re-enter future sessions" not in low


def test_transient_correction_fork_cannot_write_durable():
    """X1 ENFORCEMENT (not advice): the review fork built for a transient
    correction has NO durable memory/skill WRITE tool in its runtime whitelist.

    ``_review_tool_whitelist`` is exactly what ``_run_review_in_thread`` installs
    via ``set_thread_tool_whitelist``; ``get_pre_tool_call_block_message`` denies
    any call to a tool absent from it. So excluding ``memory`` and
    ``skill_manage`` here means the LLM fork is structurally unable to persist a
    one-off correction durably — only the deterministic ``CorrectionLearner``
    promotion path can. This replaces the prior advisory-only guard.
    """
    from agent.background_review import _review_tool_whitelist

    blocked = _review_tool_whitelist(block_durable_writes=True)
    assert "memory" not in blocked, "memory write tool must be stripped"
    assert "skill_manage" not in blocked, "skill write tool must be stripped"

    allowed = _review_tool_whitelist(block_durable_writes=False)
    # Sanity: the unblocked (durable / nudge) path still exposes the writers,
    # so we are proving a real difference, not an always-empty whitelist.
    assert "memory" in allowed
    assert "skill_manage" in allowed


def test_spawn_threads_block_flag_into_review(monkeypatch):
    """The ``block_durable_writes`` flag reaches ``_run_review_in_thread``.

    Proves the spawn wiring carries the gate end-to-end (finalize_turn ->
    _spawn_background_review -> spawn_background_review_thread -> the thread
    target), so the whitelist above is actually applied to the spawned fork.
    """
    import agent.background_review as br

    captured = {}

    def _fake_run(agent, messages_snapshot, prompt, block_durable_writes=False):
        captured["block"] = block_durable_writes

    monkeypatch.setattr(br, "_run_review_in_thread", _fake_run)
    target, _prompt = br.spawn_background_review_thread(
        agent=object(),
        messages_snapshot=[],
        review_memory=True,
        correction_hint={"kind": "DENY", "context": "x", "tier": "transient",
                         "durable": False},
        block_durable_writes=True,
    )
    target()
    assert captured["block"] is True


def test_review_preamble_durable_instructs_persist():
    from agent.background_review import _format_correction_focus

    durable = _format_correction_focus({
        "kind": "STEER", "context": "use pytest not unittest",
        "target": None, "tier": "durable", "durable": True,
    })
    low = durable.lower()
    assert "future sessions" in low or "embed" in low or "persist" in low


def test_unlearn_cli_surface_reverses_durable(tmp_path):
    """The `hermes corrections unlearn` surface actually reverses a durable item.

    Proves "reversible" is not paper-only: the CLI helper removes the durable
    line from the (fake) memory store, drops the ledger entry, and resets
    recurrence — and reports unknown ids as a non-zero exit.
    """
    from hermes_cli.corrections_cli import run_unlearn, run_list
    from agent.correction_learning import CorrectionLearner, CorrectionRecord

    sink = FakeMemorySink()
    store = tmp_path / "corrections"
    out = CorrectionLearner(store_dir=store, memory_sink=sink).record(
        CorrectionRecord(
            kind="STEER", signature="sig-cli", context="use ruff not flake8",
            session_id="s1", ts="t",
        ),
        remember=True,
    )
    pid = out["provenance_id"]
    assert "ruff" in sink.injected_text()

    # list surface runs without error while an item exists
    assert run_list(store_dir=store) == 0

    # unlearn removes it from the durable store (stops injection)
    assert run_unlearn(pid, store_dir=store, memory_sink=sink) == 0
    assert "ruff" not in sink.injected_text()
    assert CorrectionLearner(store_dir=store, memory_sink=sink).list_durable() == []

    # unknown id is a clean non-zero exit, not an exception
    assert run_unlearn("does-not-exist", store_dir=store, memory_sink=sink) == 1


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
