"""Unit tests for the tool-call log + field classifier (Slice A, #2236)."""

from __future__ import annotations

import copy
import json

import pytest
from unittest.mock import patch

from tools.tool_call_log import (
    NON_ATOMIC_TOOLS,
    ToolCallLog,
    classify_fields,
    get_default_log,
    infer_idempotency_key,
    is_non_atomic,
    register_non_atomic_tool,
    replay_or_fork,
    reset_default_log,
)


# ── is_non_atomic ───────────────────────────────────────────────────────


class TestIsNonAtomic:
    def test_registered_bare_name(self) -> None:
        assert is_non_atomic("agentmail__send_message") is True

    def test_registered_mcp_prefixed(self) -> None:
        assert is_non_atomic("mcp__agentmail__send_message") is True

    def test_case_insensitive(self) -> None:
        assert is_non_atomic("AgentMail__Send_Message") is True
        assert is_non_atomic("MCP__AgentMail__Send_Message") is True

    def test_atomic_tool_returns_false(self) -> None:
        assert is_non_atomic("web_search") is False
        assert is_non_atomic("read_file") is False
        assert is_non_atomic("mcp__tavily__search") is False

    def test_unknown_tool_returns_false(self) -> None:
        assert is_non_atomic("nonexistent_tool") is False

    def test_dynamically_registered(self) -> None:
        register_non_atomic_tool(
            "custom__irreversible",
            semantic_fields=("target",),
        )
        try:
            assert is_non_atomic("custom__irreversible") is True
            assert is_non_atomic("mcp__custom__irreversible") is True
        finally:
            NON_ATOMIC_TOOLS.pop("custom__irreversible", None)


# ── classify_fields ─────────────────────────────────────────────────────


class TestClassifyFields:
    def test_semantic_fields_captured(self) -> None:
        result = classify_fields(
            "agentmail__send_message",
            {"to": "a@b.com", "subject": "hi", "body": "hello"},
        )
        assert result.semantic == {"to": "a@b.com", "subject": "hi", "body": "hello"}
        assert result.noise == {}

    def test_noise_fields_captured(self) -> None:
        result = classify_fields(
            "agentmail__send_message",
            {"to": "a@b.com", "trace_id": "abc123", "request_id": "xyz"},
        )
        assert "to" in result.semantic
        assert "trace_id" in result.noise
        assert "request_id" in result.noise

    def test_generic_noise_patterns_detected(self) -> None:
        # nonce is a noise pattern even without an explicit registry entry.
        register_non_atomic_tool(
            "temp__tool",
            semantic_fields=("amount",),
        )
        try:
            result = classify_fields(
                "temp__tool",
                {"amount": 5, "nonce": "n1", "correlation_id": "c1"},
            )
            assert result.semantic == {"amount": 5}
            assert "nonce" in result.noise
            assert "correlation_id" in result.noise
        finally:
            NON_ATOMIC_TOOLS.pop("temp__tool", None)

    def test_unknown_field_defaults_to_noise(self) -> None:
        result = classify_fields(
            "agentmail__send_message",
            {"to": "a@b.com", "mystery_field": "x"},
        )
        assert "to" in result.semantic
        assert "mystery_field" in result.noise

    def test_case_insensitive_field_keys(self) -> None:
        result = classify_fields(
            "agentmail__send_message",
            {"TO": "a@b.com", "Trace_ID": "t"},
        )
        assert "TO" in result.semantic
        assert "Trace_ID" in result.noise

    def test_empty_arguments(self) -> None:
        result = classify_fields("agentmail__send_message", {})
        assert result.semantic == {}
        assert result.noise == {}


# ── infer_idempotency_key ───────────────────────────────────────────────


class TestIdempotencyKey:
    def test_same_intent_same_key(self) -> None:
        args_a = {"to": "a@b.com", "subject": "hi", "trace_id": "t1"}
        args_b = {"to": "a@b.com", "subject": "hi", "trace_id": "t2"}
        assert infer_idempotency_key(
            "agentmail__send_message", args_a
        ) == infer_idempotency_key("agentmail__send_message", args_b)

    def test_different_intent_different_key(self) -> None:
        args_a = {"to": "a@b.com", "subject": "hi"}
        args_b = {"to": "c@d.com", "subject": "hi"}
        assert infer_idempotency_key(
            "agentmail__send_message", args_a
        ) != infer_idempotency_key("agentmail__send_message", args_b)

    def test_key_stable_across_dict_ordering(self) -> None:
        args_a = {"to": "a@b.com", "subject": "hi"}
        args_b = {"subject": "hi", "to": "a@b.com"}
        assert infer_idempotency_key(
            "agentmail__send_message", args_a
        ) == infer_idempotency_key("agentmail__send_message", args_b)

    def test_key_includes_tool_name(self) -> None:
        args = {"name": "repo"}
        k1 = infer_idempotency_key("github__create_repo", args)
        assert k1.startswith("github__create_repo:")


# ── ToolCallLog ─────────────────────────────────────────────────────────


class TestToolCallLog:
    def test_record_and_lookup(self) -> None:
        log = ToolCallLog()
        entry = log.record(
            "agentmail__send_message",
            {"to": "a@b.com", "subject": "hi"},
        )
        assert entry.tool_name == "agentmail__send_message"
        found = log.lookup(
            "agentmail__send_message",
            {"to": "a@b.com", "subject": "hi", "trace_id": "noise"},
        )
        assert found is not None
        assert found.idempotency_key == entry.idempotency_key

    def test_has_executed_true_after_record(self) -> None:
        log = ToolCallLog()
        log.record(
            "agentmail__send_message",
            {"to": "a@b.com", "subject": "hi"},
        )
        assert log.has_executed(
            "agentmail__send_message",
            {"to": "a@b.com", "subject": "hi", "nonce": "n"},
        )

    def test_has_executed_false_for_different_intent(self) -> None:
        log = ToolCallLog()
        log.record(
            "agentmail__send_message",
            {"to": "a@b.com", "subject": "hi"},
        )
        assert not log.has_executed(
            "agentmail__send_message",
            {"to": "other@b.com", "subject": "hi"},
        )

    def test_record_atomic_tool_raises(self) -> None:
        log = ToolCallLog()
        with pytest.raises(ValueError):
            log.record("web_search", {"query": "test"})

    def test_first_writer_wins_on_repeat(self) -> None:
        log = ToolCallLog()
        first = log.record(
            "agentmail__send_message",
            {"to": "a@b.com", "subject": "first"},
        )
        # Second record with same intent but different noise — existing kept.
        second = log.record(
            "agentmail__send_message",
            {"to": "a@b.com", "subject": "first", "trace_id": "t2"},
        )
        assert second is first
        assert len(log.all_entries()) == 1

    def test_result_digest_updated_on_repeat(self) -> None:
        log = ToolCallLog()
        args = {"to": "a@b.com", "subject": "hi"}
        log.record("agentmail__send_message", args)
        before = log.lookup("agentmail__send_message", args)
        assert before is not None
        assert before.result_digest is None
        log.record("agentmail__send_message", args, result={"status": "sent"})
        after = log.lookup("agentmail__send_message", args)
        assert after is not None
        assert after.result_digest is not None

    def test_clear(self) -> None:
        log = ToolCallLog()
        log.record("agentmail__send_message", {"to": "a@b.com", "subject": "hi"})
        assert len(log.all_entries()) == 1
        log.clear()
        assert len(log.all_entries()) == 0

    def test_thread_safe_concurrent_record(self) -> None:
        import threading

        log = ToolCallLog()
        errors: list[Exception] = []

        def writer(idx: int) -> None:
            try:
                log.record(
                    "agentmail__send_message",
                    {"to": f"user{idx}@b.com", "subject": "hi"},
                )
            except Exception as exc:  # pragma: no cover — assert path
                errors.append(exc)

        threads = [threading.Thread(target=writer, args=(i,)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert errors == []
        assert len(log.all_entries()) == 10


# ── default log singleton ───────────────────────────────────────────────


class TestDefaultLog:
    def test_singleton_identity(self) -> None:
        assert get_default_log() is get_default_log()

    def test_reset_clears(self) -> None:
        log = get_default_log()
        log.record("agentmail__send_message", {"to": "x@y.com", "subject": "z"})
        assert len(log.all_entries()) >= 1
        reset_default_log()
        assert len(log.all_entries()) == 0


# ── replay-or-fork decision (Slice B, #2237) ─────────────────────────────


class TestReplayOrFork:
    def test_atomic_tool_never_replays(self) -> None:
        assert replay_or_fork("web_search", {"query": "x"}) is None

    def test_unknown_tool_never_replays(self) -> None:
        assert replay_or_fork("nonexistent_tool", {}) is None

    def test_unseen_non_atomic_forks(self) -> None:
        # No prior record -> execute normally (fork).
        assert replay_or_fork("agentmail__send_message", {"to": "a@b.com"}) is None

    def test_same_intent_after_success_replays(self) -> None:
        log = ToolCallLog()
        args = {"to": "a@b.com", "subject": "hi"}
        log.record("agentmail__send_message", args, result={"status": "sent"})
        with patch("tools.tool_call_log.get_default_log", return_value=log):
            out = replay_or_fork(
                "agentmail__send_message",
                {"to": "a@b.com", "subject": "hi", "trace_id": "noise"},
            )
        assert out is not None
        payload = json.loads(out)
        assert payload["replayed"] is True
        assert payload["tool"] == "agentmail__send_message"

    def test_different_intent_forks(self) -> None:
        log = ToolCallLog()
        log.record(
            "agentmail__send_message",
            {"to": "a@b.com", "subject": "hi"},
            result={"status": "sent"},
        )
        with patch("tools.tool_call_log.get_default_log", return_value=log):
            out = replay_or_fork(
                "agentmail__send_message", {"to": "other@b.com", "subject": "hi"}
            )
        assert out is None

    def test_recorded_but_unobserved_forks(self) -> None:
        # Recorded but result never observed (digest None) -> not yet
        # succeeded, so it must execute (fork), not replay.
        log = ToolCallLog()
        log.record("agentmail__send_message", {"to": "a@b.com", "subject": "hi"})
        with patch("tools.tool_call_log.get_default_log", return_value=log):
            out = replay_or_fork(
                "agentmail__send_message", {"to": "a@b.com", "subject": "hi"}
            )
        assert out is None


# ── dispatch-path integration (rework of #2236) ─────────────────────────


class TestDispatchIntegration:
    """invoke_tool must record non-atomic calls in the live dispatch path."""

    def test_invoke_tool_records_non_atomic(self) -> None:
        from unittest.mock import MagicMock, patch

        from agent.agent_runtime_helpers import invoke_tool

        agent = MagicMock()
        agent.session_id = "s1"
        agent._current_turn_id = "t1"
        agent._current_api_request_id = "r1"
        agent.valid_tool_names = ["agentmail__send_message"]
        agent.enabled_toolsets = None
        agent.disabled_toolsets = None
        agent._memory_manager = None  # avoid the memory-manager branch

        fake_log = ToolCallLog()
        with (
            patch("tools.tool_call_log.get_default_log", return_value=fake_log),
            patch(
                "agent.agent_runtime_helpers._ra",
                return_value=MagicMock(
                    handle_function_call=lambda *a, **k: '{"success": true}'
                ),
            ),
        ):
            invoke_tool(
                agent,
                "agentmail__send_message",
                {"to": "bob@b.com", "subject": "hi"},
                "task1",
                skip_tool_execution_middleware=True,
            )

        entries = fake_log.all_entries()
        assert len(entries) == 1
        assert entries[0].tool_name == "agentmail__send_message"

    def test_invoke_tool_skips_atomic(self) -> None:
        from unittest.mock import MagicMock, patch

        from agent.agent_runtime_helpers import invoke_tool

        agent = MagicMock()
        agent.session_id = "s1"
        agent._current_turn_id = "t1"
        agent._current_api_request_id = "r1"
        agent.valid_tool_names = ["read_file"]
        agent.enabled_toolsets = None
        agent.disabled_toolsets = None
        agent._memory_manager = None

        fake_log = ToolCallLog()
        with (
            patch("tools.tool_call_log.get_default_log", return_value=fake_log),
            patch(
                "agent.agent_runtime_helpers._ra",
                return_value=MagicMock(
                    handle_function_call=lambda *a, **k: '{"success": true}'
                ),
            ),
        ):
            invoke_tool(
                agent,
                "read_file",
                {"path": "/x"},
                "task1",
                skip_tool_execution_middleware=True,
            )

        assert fake_log.all_entries() == []
