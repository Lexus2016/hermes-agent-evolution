"""Tests for the idempotency guard (#1924).

Pure unit tests — no agent state, no MCP server, no tool execution.
Tests the classification logic, key generation, and the side-effect registry.
"""

import pytest
from agent.idempotency import (
    check_before_retry,
    idempotency_key,
    is_side_effecting_tool,
    RetryVerdict,
)


class TestIdempotencyKey:
    """Deterministic key generation from tool + args + session."""

    def test_same_inputs_same_key(self):
        args = {"to": ["a@b.com"], "subject": "hello", "inboxId": "123"}
        k1 = idempotency_key("send_message", args, "session-1")
        k2 = idempotency_key("send_message", args, "session-1")
        assert k1 == k2

    def test_different_session_different_key(self):
        args = {"to": ["a@b.com"], "subject": "hello"}
        k1 = idempotency_key("send_message", args, "session-1")
        k2 = idempotency_key("send_message", args, "session-2")
        assert k1 != k2

    def test_different_args_different_key(self):
        k1 = idempotency_key("send_message", {"subject": "a"}, "s1")
        k2 = idempotency_key("send_message", {"subject": "b"}, "s1")
        assert k1 != k2

    def test_client_id_excluded_from_key(self):
        """clientId is transport metadata, not part of the logical action."""
        args1 = {"to": ["a@b.com"], "subject": "hi", "clientId": "abc"}
        args2 = {"to": ["a@b.com"], "subject": "hi", "clientId": "xyz"}
        assert idempotency_key("send_message", args1, "s1") == idempotency_key(
            "send_message", args2, "s1"
        )

    def test_sendat_excluded_from_key(self):
        """sendAt is transport metadata for scheduled sends."""
        args1 = {"to": ["a@b.com"], "sendAt": "2025-01-01T00:00:00Z"}
        args2 = {"to": ["a@b.com"], "sendAt": "2025-06-01T00:00:00Z"}
        assert idempotency_key("send_message", args1, "s1") == idempotency_key(
            "send_message", args2, "s1"
        )

    def test_key_is_hex(self):
        key = idempotency_key("any_tool", {}, "s1")
        assert len(key) == 32
        int(key, 16)  # should not raise


class TestIsSideEffectingTool:
    def test_registered_side_effecting_tools(self):
        assert is_side_effecting_tool("mcp__murable__agentmail__send_message")
        assert is_side_effecting_tool("mcp__murable__agentmail__create_draft")
        assert is_side_effecting_tool("mcp__murable__github__create_issue")
        assert is_side_effecting_tool("mcp__murable__x_twitter__create_tweet")

    def test_non_side_effecting(self):
        assert not is_side_effecting_tool("read_file")
        assert not is_side_effecting_tool("terminal")
        assert not is_side_effecting_tool("search_files")
        assert not is_side_effecting_tool("patch")
        assert not is_side_effecting_tool("mcp__murable__agentmail__list_messages")


class TestCheckBeforeRetry:
    """The core verdict function."""

    def test_non_side_effecting_returns_none(self):
        """Non-side-effecting tools are not an atomicity concern."""
        result = check_before_retry("read_file", {}, "timed out")
        assert result is None

    def test_normal_error_returns_none(self):
        """Non-timeout errors on side-effecting tools are retryable."""
        result = check_before_retry(
            "mcp__murable__agentmail__send_message",
            {"inboxId": "123", "to": ["a@b.com"]},
            "Error: invalid recipient address",
        )
        assert result is None

    def test_timeout_on_email_returns_do_not_retry_verdict(self):
        """Post-dispatch timeout on email → verify-before-retry directive."""
        result = check_before_retry(
            "mcp__murable__agentmail__send_message",
            {"inboxId": "123", "to": ["a@b.com"]},
            "Error executing tool 'send_message': timed out after 30.0s",
        )
        assert result is not None
        assert isinstance(result, RetryVerdict)
        assert "do NOT retry" in result.feedback
        assert result.effect_type == "email_send"
        assert "list_messages" in result.verify_tool

    def test_connection_reset_on_github_issue(self):
        """Connection reset after issue creation → verify-before-retry."""
        result = check_before_retry(
            "mcp__murable__github__create_issue",
            {"owner": "org", "repo": "r", "title": "t"},
            "Connection reset by peer",
        )
        assert result is not None
        assert result.effect_type == "github_issue"
        assert "list_issues" in result.verify_tool

    def test_deadline_exceeded_on_tweet(self):
        """Deadline exceeded on tweet → verify-before-retry."""
        result = check_before_retry(
            "mcp__murable__x_twitter__create_tweet",
            {"text": "hello"},
            "deadline exceeded",
        )
        assert result is not None
        assert result.effect_type == "tweet"
        assert "get_user_tweets" in result.verify_tool

    def test_broken_pipe_on_email(self):
        """Broken pipe is a post-dispatch error."""
        result = check_before_retry(
            "mcp__murable__agentmail__send_message",
            {"inboxId": "123"},
            "[Errno 32] Broken pipe",
        )
        assert result is not None

    def test_non_string_result_returns_none(self):
        """Non-string results (dicts, lists) are not checked."""
        result = check_before_retry(
            "mcp__murable__agentmail__send_message",
            {},
            {"error": "timed out"},
        )
        assert result is None

    def test_draft_timeout(self):
        """Draft creation timeout → verify-before-retry."""
        result = check_before_retry(
            "mcp__murable__agentmail__create_draft",
            {"inboxId": "123"},
            "Operation timed out",
        )
        assert result is not None
        assert result.effect_type == "email_draft"

    def test_feedback_mentions_verify_tool(self):
        """The feedback must tell the model which tool to use for verification."""
        result = check_before_retry(
            "mcp__murable__github__create_issue",
            {"owner": "o", "repo": "r"},
            "Connection closed",
        )
        assert result is not None
        assert "list_issues" in result.feedback

    def test_feedback_contains_idempotency_tag(self):
        """The directive must be tagged [idempotency] for visibility."""
        result = check_before_retry(
            "mcp__murable__agentmail__send_message",
            {},
            "timed out",
        )
        assert result is not None
        assert "[idempotency]" in result.feedback
