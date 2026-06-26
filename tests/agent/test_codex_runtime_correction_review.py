"""Codex-runtime parity for learn-from-corrections (Phase 1).

Before the shared ``agent/correction_review.py`` seam, the Codex app-server
finalizer (``run_codex_app_server_turn``) carried an unmodified nudge-only gate:
it never detected or recorded a user correction, so the whole feature was a
silent no-op on the Codex runtime. These tests prove the Codex path now routes
through the SAME decision as the default ``finalize_turn``:

* a DENY correction is detected + RECORDED deterministically (the headline
  parity fix), and
* the spawn / block rules match the default finalizer (no fork for a pure
  transient correction without a nudge; fork for a durable correction; nudge-
  only behavior unchanged).
"""

from __future__ import annotations

import json

from agent.codex_runtime import run_codex_app_server_turn


class _FakeTurn:
    def __init__(self, *, final_text="ok", interrupted=False, error=None):
        self.final_text = final_text
        self.interrupted = interrupted
        self.error = error
        self.should_retire = False
        self.projected_messages = []
        self.tool_iterations = 0
        self.token_usage_last = None  # forces the usage helper's no-usage branch
        self.model_context_window = None
        self.thread_id = "thread-1"
        self.turn_id = "turn-1"


class _FakeSession:
    def __init__(self, turn):
        self._turn = turn

    def run_turn(self, *, user_input):
        return self._turn

    def close(self):
        pass


class _CodexStubAgent:
    def __init__(self, turn):
        self._codex_session = _FakeSession(turn)
        self._iters_since_skill = 0
        self._skill_nudge_interval = 0
        self.valid_tool_names = {"skill_manage"}
        self.session_api_calls = 0
        self._session_db = None
        self._session_db_created = False
        self.session_id = "sess-1"
        self.model = "codex/model"
        self.provider = "openai"
        self.base_url = "http://stub"
        self._interrupt_message = None
        self.context_compressor = None
        self.spawned = []
        self.recorded = []

    def _sync_external_memory_for_turn(self, **k):
        pass

    def _record_turn_correction(self, hint):
        self.recorded.append(hint)
        return self._record_outcome

    # default recorder outcome — overridden per test
    _record_outcome = {"tier": "transient", "durable": False}

    def _spawn_background_review(self, *, messages_snapshot, review_memory,
                                review_skills, correction_hint=None,
                                block_durable_writes=False):
        self.spawned.append({
            "review_memory": review_memory,
            "review_skills": review_skills,
            "correction_hint": correction_hint,
            "block_durable_writes": block_durable_writes,
        })


def _deny_messages():
    return [
        {"role": "user", "content": "clean up"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "c1", "function": {"name": "terminal", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "c1", "content": json.dumps(
            {"error": "Command denied: rm -rf build", "status": "blocked",
             "user_denied": True})},
    ]


def _normal_messages():
    return [
        {"role": "user", "content": "do a thing"},
        {"role": "assistant", "content": "done"},
    ]


def _drive(agent, messages, *, should_review_memory=False):
    return run_codex_app_server_turn(
        agent,
        user_message="clean up",
        original_user_message="clean up",
        messages=messages,
        effective_task_id="task-1",
        should_review_memory=should_review_memory,
    )


def test_codex_path_detects_and_records_correction():
    # Headline parity fix: a denial on the Codex runtime is now detected and
    # recorded deterministically (it never was before). No nudge -> no fork.
    turn = _FakeTurn(final_text="ok")
    agent = _CodexStubAgent(turn)
    agent._record_outcome = {"tier": "transient", "durable": False}
    _drive(agent, _deny_messages(), should_review_memory=False)
    assert len(agent.recorded) == 1
    assert agent.recorded[0]["kind"] == "DENY"
    assert agent.spawned == []  # pure transient, no nudge -> no wasted fork


def test_codex_path_durable_correction_spawns_fork():
    # A promoted (durable) correction spawns the fork even with no nudge, and
    # keeps durable-write capability (block False) — same rule as the default.
    turn = _FakeTurn(final_text="ok")
    agent = _CodexStubAgent(turn)
    agent._record_outcome = {"tier": "durable", "durable": True}
    _drive(agent, _deny_messages(), should_review_memory=False)
    assert len(agent.spawned) == 1
    assert agent.spawned[0]["correction_hint"]["kind"] == "DENY"
    assert agent.spawned[0]["block_durable_writes"] is False


def test_codex_path_transient_correction_with_nudge_blocks_writes():
    # Transient correction co-occurring with a memory nudge: fork spawns but
    # durable writes are blocked (universal X1).
    turn = _FakeTurn(final_text="ok")
    agent = _CodexStubAgent(turn)
    agent._record_outcome = {"tier": "transient", "durable": False}
    _drive(agent, _deny_messages(), should_review_memory=True)
    assert len(agent.spawned) == 1
    assert agent.spawned[0]["block_durable_writes"] is True


def test_codex_path_nudge_only_unchanged():
    # No correction + a nudge -> fork spawns with no hint, no block (pre-existing
    # codex behavior preserved).
    turn = _FakeTurn(final_text="ok")
    agent = _CodexStubAgent(turn)
    _drive(agent, _normal_messages(), should_review_memory=True)
    assert len(agent.spawned) == 1
    assert agent.spawned[0]["correction_hint"] is None
    assert agent.spawned[0]["block_durable_writes"] is False
    assert agent.recorded == []


def test_codex_path_normal_turn_no_nudge_no_fork():
    # No correction, no nudge -> nothing recorded, no fork.
    turn = _FakeTurn(final_text="ok")
    agent = _CodexStubAgent(turn)
    _drive(agent, _normal_messages(), should_review_memory=False)
    assert agent.recorded == []
    assert agent.spawned == []
