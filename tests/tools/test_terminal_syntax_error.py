"""Tests for the shell_syntax_error failure category (#2243).

The terminal failure classifier detects shell/interpreter syntax errors
(malformed quoting, unmatched delimiters, bad tokens) and classifies them as
``shell_syntax_error`` — a deterministic, non-retryable category distinct from
the generic ``persistent_error``.  These errors reproduce identically on every
run, so retrying the exact same command wastes turns.  The hint directs the
agent to fix the quoting/escaping or write the command to a script file.
"""

import pytest

from tools.terminal_failure_classifier import (
    FailureCategory,
    classify_terminal_failure,
)


class TestShellSyntaxErrorClassification:
    """Syntax errors classify as ``shell_syntax_error`` with should_retry=False."""

    @pytest.mark.parametrize(
        "message",
        [
            "bash: syntax error near unexpected token `('",
            "sh: syntax error: unexpected end of file",
            "bash: SyntaxError: invalid syntax",
            "node: unexpected token }",
            "python: SyntaxError: invalid syntax",
            "zsh: parse error near `)'",
        ],
    )
    def test_syntax_error_messages_classify_correctly(self, message):
        result = classify_terminal_failure(
            command="bad --command (here",
            exit_code=2,
            stdout="",
            stderr=message,
        )
        assert result.category == FailureCategory.shell_syntax_error
        assert result.should_retry is False

    def test_interpreter_exit2_without_text_classifies_as_syntax_error(self):
        """exit_code==2 from a known interpreter (python) is a syntax error
        even when the output text doesn't contain a literal 'syntax error'."""
        result = classify_terminal_failure(
            command="python3 -c 'print('",
            exit_code=2,
            stdout="",
            stderr='  File "<string>", line 1\n    print(\n         ^\n'
            "IndentationError: unexpected indent",
        )
        assert result.category == FailureCategory.shell_syntax_error
        assert result.should_retry is False

    def test_hint_mentions_quoting_and_script_file(self):
        result = classify_terminal_failure(
            command='echo "hello',
            exit_code=2,
            stdout="",
            stderr="bash: syntax error: unexpected end of file",
        )
        assert "quoting" in result.hint.lower()
        assert "write_file" in result.hint or "script file" in result.hint.lower()

    def test_bash_unterminated_quote(self):
        """A real bash unterminated-quote error message."""
        result = classify_terminal_failure(
            command='echo "hello world',
            exit_code=2,
            stdout="",
            stderr="bash: unexpected EOF while looking for matching `\"'",
        )
        assert result.category == FailureCategory.shell_syntax_error
        assert result.should_retry is False


class TestNoRegressionOtherCategories:
    """Other failure categories are NOT misclassified as shell_syntax_error."""

    def test_command_not_found_still_missing_command(self):
        result = classify_terminal_failure(
            command="nonexistent_binary_xyz",
            exit_code=127,
            stdout="",
            stderr="bash: nonexistent_binary_xyz: command not found",
        )
        assert result.category == FailureCategory.missing_command

    def test_permission_denied_still_permission_denied(self):
        result = classify_terminal_failure(
            command="cat /root/secret",
            exit_code=126,
            stdout="",
            stderr="cat: /root/secret: Permission denied",
        )
        assert result.category == FailureCategory.permission_denied

    def test_timeout_still_timeout(self):
        """A text-based timeout (not exit_code 124, which is always
        deterministic per #2191) on first occurrence is retryable."""
        result = classify_terminal_failure(
            command="curl http://10.255.255.1",
            exit_code=28,
            stdout="",
            stderr="curl: (28) Connection timed out after 1000 milliseconds",
            consecutive_count=0,
        )
        assert result.category == FailureCategory.timeout

    def test_transient_network_error_still_retryable(self):
        result = classify_terminal_failure(
            command="curl http://example.invalid",
            exit_code=6,
            stdout="",
            stderr="curl: (6) Could not resolve host: example.invalid",
        )
        assert result.category == FailureCategory.retryable_transient
        assert result.should_retry is True

    def test_runtime_error_not_syntax_error(self):
        """A command that runs but fails at runtime is NOT a syntax error."""
        result = classify_terminal_failure(
            command="ls /nonexistent/directory",
            exit_code=2,
            stdout="",
            stderr="ls: cannot access '/nonexistent/directory': No such file or directory",
        )
        # ls exit 2 is a runtime error (file not found), not a syntax error.
        assert result.category != FailureCategory.shell_syntax_error
