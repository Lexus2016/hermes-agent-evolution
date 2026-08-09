"""Tests for refusal recovery (#2168) and write_file parse-error spiral (#2169).

Covers:
- ``tool_diagnostics.classify`` produces a ``refusal`` category for HTTP 403 /
  access-blocked / request-refused patterns (#2168).
- ``loop_guard._is_non_retryable`` treats ``refusal`` as non-retryable so the
  circuit breaker fires after 2 consecutive occurrences (#2168).
- ``tool_error_recovery.classify_tool_error`` maps write_file validation/parse
  errors to ``permanent``/``abort`` instead of ``validation``/``fix_args`` so
  the agent gets a deterministic-failure hint with a fallback directive (#2169).
- ``tool_error_recovery.classify_tool_error`` maps permission errors to
  ``use_alternative`` with a fallback chain hint (#2168).
- ``tool_diagnostics.classify`` parse_error hint includes a fallback directive
  to use ``terminal`` with a heredoc (#2169).
"""

from __future__ import annotations

from agent.loop_guard import _is_non_retryable
from agent.tool_diagnostics import classify, hint_for
from agent.tool_error_recovery import (
    RecoveryAction,
    ToolErrorClass,
    classify_tool_error,
    refine_classification,
    ToolFailure,
)


# ── #2168: refusal category in tool_diagnostics ──────────────────────────


class TestRefusalClassification:
    """tool_diagnostics.classify should produce a 'refusal' category for
    backend-refused / HTTP 403 / access-blocked patterns."""

    def test_http_403_classified_as_refusal(self):
        result = classify("HTTP 403: Forbidden — access blocked by server")
        assert result is not None
        category, hint = result
        assert category == "refusal"
        assert "refused" in hint.lower() or "alternative" in hint.lower()

    def test_access_blocked_classified_as_refusal(self):
        result = classify("Error: access blocked by firewall policy")
        assert result is not None
        category, _ = result
        assert category == "refusal"

    def test_request_refused_classified_as_refusal(self):
        result = classify("Request refused: operation not allowed in this context")
        assert result is not None
        category, _ = result
        assert category == "refusal"

    def test_refusal_hint_mentioned_alternatives(self):
        result = classify("403 forbidden: access blocked")
        assert result is not None
        _, hint = result
        assert "alternative" in hint.lower()
        assert "retry" in hint.lower()

    def test_pure_permission_denied_still_permission(self):
        """Filesystem 'permission denied' should still classify as 'permission',
        not 'refusal' — the refusal rule matches backend-level rejections."""
        result = classify("permission denied: /root/secret/file.txt")
        assert result is not None
        category, _ = result
        assert category == "permission"

    def test_refusal_is_non_retryable(self):
        """loop_guard should treat 'refusal' as non-retryable (#2168)."""
        assert _is_non_retryable("web_search", "refusal") is True
        assert _is_non_retryable("terminal", "refusal") is True
        assert _is_non_retryable("any_tool", "refusal") is True

    def test_permission_still_non_retryable(self):
        """The existing 'permission' category remains non-retryable."""
        assert _is_non_retryable("write_file", "permission") is True

    def test_refusal_hint_retrievable(self):
        """hint_for('refusal') returns the enriched hint."""
        hint = hint_for("refusal")
        assert hint is not None
        assert "alternative" in hint.lower()


# ── #2169: write_file parse-error → permanent/abort ──────────────────────


class TestWriteFileParseErrorSpiral:
    """write_file parse/validation errors should be classified as
    permanent/abort with a fallback directive (#2169)."""

    def test_write_file_validation_is_permanent(self):
        result = classify_tool_error("write_file", "JSONDecodeError: invalid JSON")
        assert result.error_class == ToolErrorClass.permanent
        assert result.recovery_action == RecoveryAction.abort

    def test_write_file_parse_error_hint_mentions_alternative(self):
        result = classify_tool_error("write_file", "syntax error: unexpected token")
        assert "terminal" in result.hint.lower()
        assert "blind-retry" in result.hint.lower() or "do not" in result.hint.lower()

    def test_non_write_file_validation_stays_validation(self):
        """For tools other than write_file, validation errors stay as
        validation/fix_args (not permanent/abort)."""
        result = classify_tool_error("patch", "Invalid arguments: expected str")
        assert result.error_class == ToolErrorClass.validation
        assert result.recovery_action == RecoveryAction.fix_args

    def test_write_file_permission_uses_alternative(self):
        """write_file permission errors should use use_alternative action
        with a fallback chain hint (#2168)."""
        result = classify_tool_error("write_file", "Permission denied: /root/secret")
        assert result.recovery_action == RecoveryAction.use_alternative
        assert "alternative" in result.hint.lower()

    def test_refine_classification_write_file_validation(self):
        """Directly test refine_classification for write_file + validation."""
        failure = ToolFailure(
            tool_name="write_file",
            error_message="YAML decode error",
            error_class=ToolErrorClass.validation,
            recovery_action=RecoveryAction.fix_args,
            hint="Fix the arguments.",
        )
        refined = refine_classification(failure)
        assert refined.error_class == ToolErrorClass.permanent
        assert refined.recovery_action == RecoveryAction.abort
        assert "terminal" in refined.hint.lower()

    def test_refine_classification_permission(self):
        """Directly test refine_classification for permission errors."""
        failure = ToolFailure(
            tool_name="terminal",
            error_message="Permission denied",
            error_class=ToolErrorClass.permission,
            recovery_action=RecoveryAction.check_credentials,
            hint="Check credentials.",
        )
        refined = refine_classification(failure)
        assert refined.recovery_action == RecoveryAction.use_alternative
        assert "alternative" in refined.hint.lower()

    def test_refine_classification_noop_for_unknown(self):
        """refine_classification should be a no-op for unknown errors."""
        failure = ToolFailure(
            tool_name="custom",
            error_message="something weird",
            error_class=ToolErrorClass.unknown,
            recovery_action=RecoveryAction.escalate,
            hint="",
        )
        refined = refine_classification(failure)
        assert refined.error_class == ToolErrorClass.unknown
        assert refined.recovery_action == RecoveryAction.escalate


# ── #2169: parse_error hint in tool_diagnostics includes fallback ────────


class TestParseErrorHintEnrichment:
    """The tool_diagnostics parse_error hint should mention the terminal
    fallback directive (#2169)."""

    def test_parse_error_hint_mentions_terminal(self):
        result = classify("JSONDecodeError: invalid JSON at line 5")
        assert result is not None
        category, hint = result
        assert category == "parse_error"
        assert "terminal" in hint.lower()

    def test_parse_error_hint_mentions_heredoc(self):
        result = classify("syntax validation: file fails .yaml syntax check")
        assert result is not None
        _, hint = result
        assert "heredoc" in hint.lower()