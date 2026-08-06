"""Tests for agent.failed_attempt_marker (#1580 — Slice A).

Covers:
  - Pure span detection over representative message shapes.
  - Failure classifier reuse (classify / payload_anomaly / interrupted).
  - Conservative behaviour: successful attempts are NOT flagged.
  - Edge cases: empty list, dangling tool_calls, multimodal content.
"""

from __future__ import annotations

import pytest

from agent.failed_attempt_marker import (
    FailedAttemptSpan,
    failed_attempt_indices,
    identify_failed_attempt_spans,
)


# ---------------------------------------------------------------------------
# Message builders
# ---------------------------------------------------------------------------


def _assistant(tool_calls=None, content="Thinking..."):
    """Build an assistant message, optionally with tool_calls."""
    msg = {"role": "assistant", "content": content}
    if tool_calls:
        msg["tool_calls"] = tool_calls
    return msg


def _tool_result(call_id: str, content):
    return {"role": "tool", "tool_call_id": call_id, "content": content}


def _tc(call_id: str, name: str = "terminal", args: str = "{}"):
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": args},
    }


def _user(text: str = "Do the thing"):
    return {"role": "user", "content": text}


# ---------------------------------------------------------------------------
# identify_failed_attempt_spans
# ---------------------------------------------------------------------------


class TestIdentifyFailedAttemptSpans:
    """Core span-detection logic."""

    def test_empty_messages(self):
        assert identify_failed_attempt_spans([]) == []

    def test_no_tool_calls(self):
        msgs = [_user(), _assistant(content="Hello")]
        assert identify_failed_attempt_spans(msgs) == []

    def test_successful_attempt_not_flagged(self):
        """A fully successful assistant→tool block is NOT a failed span."""
        msgs = [
            _user(),
            _assistant(tool_calls=[_tc("c1")]),
            _tool_result("c1", "command succeeded\nexit code 0"),
        ]
        assert identify_failed_attempt_spans(msgs) == []

    def test_runtime_error_flagged(self):
        msgs = [
            _user(),
            _assistant(tool_calls=[_tc("c1")]),
            _tool_result("c1", "Traceback (most recent call last):\n  File ... Error"),
        ]
        spans = identify_failed_attempt_spans(msgs)
        assert len(spans) == 1
        assert spans[0].assistant_index == 1
        assert 2 in spans[0].result_indices
        assert spans[0].failure_categories[2][1] == "classify"

    def test_permission_error_flagged(self):
        msgs = [
            _assistant(tool_calls=[_tc("c1")]),
            _tool_result("c1", "permission denied: /root/secret"),
        ]
        spans = identify_failed_attempt_spans(msgs)
        assert len(spans) == 1
        cat, source = spans[0].failure_categories[1]
        assert cat == "permission"
        assert source == "classify"

    def test_timeout_flagged(self):
        msgs = [
            _assistant(tool_calls=[_tc("c1")]),
            _tool_result("c1", "Error: timed out after 30s"),
        ]
        spans = identify_failed_attempt_spans(msgs)
        assert len(spans) == 1
        cat, _ = spans[0].failure_categories[1]
        assert cat == "timeout"

    def test_not_found_flagged(self):
        msgs = [
            _assistant(tool_calls=[_tc("c1")]),
            _tool_result("c1", "grep: no such file or directory"),
        ]
        spans = identify_failed_attempt_spans(msgs)
        assert len(spans) == 1

    def test_empty_payload_flagged(self):
        """payload_anomaly catches structurally-broken results."""
        msgs = [
            _assistant(tool_calls=[_tc("c1")]),
            _tool_result("c1", ""),
        ]
        spans = identify_failed_attempt_spans(msgs)
        assert len(spans) == 1
        cat, source = spans[0].failure_categories[1]
        assert source == "payload_anomaly"

    def test_null_payload_flagged(self):
        msgs = [
            _assistant(tool_calls=[_tc("c1")]),
            _tool_result("c1", "null"),
        ]
        spans = identify_failed_attempt_spans(msgs)
        assert len(spans) == 1

    def test_interrupted_flagged(self):
        msgs = [
            _assistant(tool_calls=[_tc("c1")]),
            _tool_result("c1", "[command interrupted]\nexit_code: 130"),
        ]
        spans = identify_failed_attempt_spans(msgs)
        assert len(spans) == 1
        cat, source = spans[0].failure_categories[1]
        assert source == "interrupted"

    def test_mixed_success_failure_flagged(self):
        """If ANY result in a multi-call block fails, the whole span flags."""
        msgs = [
            _assistant(tool_calls=[_tc("c1"), _tc("c2")]),
            _tool_result("c1", "OK\nexit code 0"),
            _tool_result("c2", "permission denied"),
        ]
        spans = identify_failed_attempt_spans(msgs)
        assert len(spans) == 1
        span = spans[0]
        assert span.assistant_index == 0
        assert len(span.result_indices) == 2
        # Only c2 (index 2) is a failure
        assert 2 in span.failure_categories
        assert 1 not in span.failure_categories

    def test_multiple_independent_spans(self):
        msgs = [
            _assistant(tool_calls=[_tc("c1")]),
            _tool_result("c1", "Error: something failed"),
            _assistant(tool_calls=[_tc("c2")]),
            _tool_result("c2", "All good\nexit code 0"),
            _assistant(tool_calls=[_tc("c3")]),
            _tool_result("c3", "Traceback (most recent call last): ValueError"),
        ]
        spans = identify_failed_attempt_spans(msgs)
        assert len(spans) == 2
        assert spans[0].assistant_index == 0
        assert spans[1].assistant_index == 4

    def test_dangling_tool_calls_skipped(self):
        """assistant(tool_calls) with NO following tool results is skipped."""
        msgs = [
            _user(),
            _assistant(tool_calls=[_tc("c1")]),
            _user("next message"),
        ]
        assert identify_failed_attempt_spans(msgs) == []

    def test_all_indices_property(self):
        msgs = [
            _assistant(tool_calls=[_tc("c1"), _tc("c2")]),
            _tool_result("c1", "error: failed"),
            _tool_result("c2", "None"),
        ]
        spans = identify_failed_attempt_spans(msgs)
        assert len(spans) == 1
        assert spans[0].all_indices == (0, 1, 2)

    def test_multimodal_content_supported(self):
        """List-shaped content (multimodal) is flattened before classification."""
        msgs = [
            _assistant(tool_calls=[_tc("c1")]),
            _tool_result("c1", [{"type": "text", "text": "Error: exit code 1"}]),
        ]
        spans = identify_failed_attempt_spans(msgs)
        assert len(spans) == 1


# ---------------------------------------------------------------------------
# failed_attempt_indices
# ---------------------------------------------------------------------------


class TestFailedAttemptIndices:
    """Convenience frozenset API."""

    def test_empty(self):
        assert failed_attempt_indices([]) == frozenset()

    def test_returns_frozenset(self):
        msgs = [
            _assistant(tool_calls=[_tc("c1")]),
            _tool_result("c1", "permission denied"),
        ]
        result = failed_attempt_indices(msgs)
        assert isinstance(result, frozenset)
        assert result == frozenset({0, 1})

    def test_exclude_assistant(self):
        msgs = [
            _assistant(tool_calls=[_tc("c1")]),
            _tool_result("c1", "Traceback (most recent call last): error"),
        ]
        result = failed_attempt_indices(msgs, include_assistant=False)
        assert 0 not in result
        assert 1 in result

    def test_multiple_spans_combined(self):
        msgs = [
            _assistant(tool_calls=[_tc("c1")]),
            _tool_result("c1", "error: failed"),
            _assistant(tool_calls=[_tc("c2")]),
            _tool_result("c2", "timed out"),
        ]
        result = failed_attempt_indices(msgs)
        assert result == frozenset({0, 1, 2, 3})


# ---------------------------------------------------------------------------
# FailedAttemptSpan
# ---------------------------------------------------------------------------


class TestFailedAttemptSpan:
    """Structural correctness of the span data class."""

    def test_repr(self):
        span = FailedAttemptSpan(
            assistant_index=0,
            result_indices=[1],
            failure_categories={1: ("timeout", "classify")},
        )
        r = repr(span)
        assert "assistant=0" in r
        assert "results=(1,)" in r

    def test_equality(self):
        s1 = FailedAttemptSpan(0, [1], {1: ("timeout", "classify")})
        s2 = FailedAttemptSpan(0, [1], {1: ("timeout", "classify")})
        assert s1 == s2

    def test_inequality(self):
        s1 = FailedAttemptSpan(0, [1], {1: ("timeout", "classify")})
        s2 = FailedAttemptSpan(0, [1], {1: ("permission", "classify")})
        assert s1 != s2

    def test_hashable(self):
        span = FailedAttemptSpan(0, [1], {1: ("timeout", "classify")})
        assert hash(span) is not None
