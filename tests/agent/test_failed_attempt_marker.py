"""Tests for failed-attempt detection and context denoising (#1580).

Verifies:
1. ``failed_attempt_indices`` correctly identifies error-bearing tool results.
2. ``ContextCompressor._prune_old_tool_results`` prioritises failed attempts
   for removal — they are demoted *before* the general pruning pass runs.
"""

from __future__ import annotations

import sys
from unittest.mock import patch

import pytest

from agent.failed_attempt_marker import failed_attempt_indices


# ---------------------------------------------------------------------------
# Unit tests for failed_attempt_indices
# ---------------------------------------------------------------------------


class TestFailedAttemptIndices:
    """Direct unit tests for the predicate."""

    def test_empty_messages(self):
        assert failed_attempt_indices([]) == []

    def test_no_tool_messages(self):
        msgs = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi"},
        ]
        assert failed_attempt_indices(msgs) == []

    def test_successful_tool_result_not_flagged(self):
        msgs = [
            {"role": "tool", "content": "x" * 200, "tool_call_id": "c1"},
        ]
        assert failed_attempt_indices(msgs) == []

    def test_traceback_detected(self):
        content = (
            "Traceback (most recent call last):\n"
            '  File "/app/foo.py", line 10, in <module>\n'
            "    raise ValueError('bad')\n"
            "ValueError: bad\n"
        )
        msgs = [{"role": "tool", "content": content, "tool_call_id": "c1"}]
        assert failed_attempt_indices(msgs) == [0]

    def test_nonzero_exit_code_detected(self):
        content = '{"output": "", "error": "command failed", "exit_code": 1}'
        msgs = [{"role": "tool", "content": content, "tool_call_id": "c1"}]
        assert failed_attempt_indices(msgs) == [0]

    def test_error_json_key_detected(self):
        content = '{"status": "error", "error": "FileNotFoundError: /tmp/missing.txt"}'
        msgs = [{"role": "tool", "content": content, "tool_call_id": "c1"}]
        assert failed_attempt_indices(msgs) == [0]

    def test_loop_guard_detected(self):
        content = (
            "[loop-guard] The `terminal` tool (mutating) has failed "
            "2 times in a row with the same approach. STOP repeating it."
        )
        msgs = [{"role": "tool", "content": content, "tool_call_id": "c1"}]
        assert failed_attempt_indices(msgs) == [0]

    def test_short_content_not_flagged(self):
        """Very short results (<30 chars) are not flagged to avoid FPs."""
        msgs = [{"role": "tool", "content": "Error: nope", "tool_call_id": "c1"}]
        assert failed_attempt_indices(msgs) == []

    def test_multimodal_text_part_with_error(self):
        content = [
            {
                "type": "text",
                "text": "Process exited with code 127 — command not found",
            },
        ]
        msgs = [{"role": "tool", "content": content, "tool_call_id": "c1"}]
        assert failed_attempt_indices(msgs) == [0]

    def test_dict_error_field(self):
        content = {"error": "something went wrong and here is a longer message"}
        msgs = [{"role": "tool", "content": content, "tool_call_id": "c1"}]
        assert failed_attempt_indices(msgs) == [0]

    def test_multiple_messages_mixed(self):
        ok = "x" * 300
        err = "Traceback (most recent call last):\nValueError: bad\n" + "z" * 100
        msgs = [
            {"role": "user", "content": "do something"},
            {"role": "tool", "content": ok, "tool_call_id": "c1"},
            {"role": "assistant", "content": "next"},
            {"role": "tool", "content": err, "tool_call_id": "c2"},
            {"role": "user", "content": "again"},
        ]
        assert failed_attempt_indices(msgs) == [3]


# ---------------------------------------------------------------------------
# Integration tests: _prune_old_tool_results prioritises failed attempts
# ---------------------------------------------------------------------------


@pytest.fixture
def stub_compressor():
    """Minimal ContextCompressor — only _prune_old_tool_results is needed."""
    from agent.context_compressor import ContextCompressor

    # Bypass __init__ (which needs a config/client) — we only call one method.
    c = ContextCompressor.__new__(ContextCompressor)
    return c


class TestFailedAttemptPruning:
    """Verify that failed tool results are demoted before successful ones."""

    def test_failed_result_pruned_before_successful(self, stub_compressor):
        """Both results are large and in the pruneable region, but the
        failed one should be demoted first (Pass 1b) so its verbose error
        output becomes a 1-line summary."""
        ok_content = "def hello():\n    return 'world'\n" + "x" * 500
        err_content = (
            "Traceback (most recent call last):\n"
            '  File "/app/main.py", line 42\n'
            "    result = do_thing()\n"
            "ConnectionError: refused\n" + "detail " * 80
        )
        messages = [
            {"role": "tool", "content": err_content, "tool_call_id": "call_err"},
            {"role": "tool", "content": ok_content, "tool_call_id": "call_ok"},
            {
                "role": "assistant",
                "content": "done",
                "tool_calls": [
                    {
                        "id": "call_err",
                        "type": "function",
                        "function": {
                            "name": "terminal",
                            "arguments": '{"command": "make test"}',
                        },
                    },
                    {
                        "id": "call_ok",
                        "type": "function",
                        "function": {
                            "name": "read_file",
                            "arguments": '{"path": "main.py"}',
                        },
                    },
                ],
            },
            {"role": "user", "content": "continue"},
        ]
        result, pruned = stub_compressor._prune_old_tool_results(
            messages, protect_tail_count=2
        )
        # Both should be pruned, but the key assertion is that the error
        # result was demoted — its content should no longer be the full traceback.
        assert pruned >= 1
        assert "Traceback" not in result[0]["content"]

    def test_failed_result_in_protected_tail_not_pruned(self, stub_compressor):
        """A failed result inside the protected tail should survive."""
        err_content = (
            "Traceback (most recent call last):\nValueError: bad\n" + "z" * 300
        )
        messages = [
            {"role": "assistant", "content": "start"},
            {"role": "tool", "content": err_content, "tool_call_id": "c1"},
            {"role": "user", "content": "continue"},
        ]
        result, pruned = stub_compressor._prune_old_tool_results(
            messages, protect_tail_count=2
        )
        # Tool result is at index 1, prune_boundary = 3 - 2 = 1, so index 1
        # is NOT in the pruneable region (i < prune_boundary is False).
        assert pruned == 0
        assert result[1]["content"] == err_content

    def test_no_failed_attempts_no_extra_pruning(self, stub_compressor):
        """When there are no failed attempts, behaviour is unchanged."""
        ok_content = "x" * 500
        messages = [
            {"role": "tool", "content": ok_content, "tool_call_id": "c1"},
            {
                "role": "assistant",
                "content": "done",
                "tool_calls": [
                    {
                        "id": "c1",
                        "type": "function",
                        "function": {"name": "read_file", "arguments": "{}"},
                    },
                ],
            },
            {"role": "user", "content": "continue"},
        ]
        result, pruned = stub_compressor._prune_old_tool_results(
            messages, protect_tail_count=2
        )
        # Standard Pass 2 prunes the large result
        assert pruned == 1
