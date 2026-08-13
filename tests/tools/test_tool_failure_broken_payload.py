"""Tests for the broken-payload (USR) failure category (#1495).

Verifies that the tool-failure classifier recognizes the ``[tool_error]``
signal injected by ``make_tool_result_message`` when ``payload_anomaly()``
flags a structurally broken tool payload, and that it maps to the
``broken_payload`` category with a retryable disposition and a hint that
explicitly warns against fabricating a safety rationale (Unfaithful Safety
Refusal).
"""

from __future__ import annotations

from tools.tool_failure_classifier import (
    ToolFailureCategory,
    classify_tool_failure,
    matched_categories,
)


class TestBrokenPayloadClassification:
    """The classifier must surface the USR signal as broken_payload."""

    def test_tool_error_signal_classifies_as_broken_payload(self) -> None:
        result = classify_tool_failure(
            "web_search",
            "[tool_error] Broken payload (type=empty_payload). "
            "The tool returned an empty/null payload — it likely malfunctioned.",
        )
        assert result.category == ToolFailureCategory.broken_payload
        assert result.should_retry

    def test_hint_warns_against_safety_fabrication(self) -> None:
        result = classify_tool_failure(
            "mcp_tool",
            "[tool_error] Broken payload (type=malformed_payload).",
        )
        assert "Do NOT fabricate a safety rationale" in result.hint
        assert "Unfaithful Safety Refusal" in result.hint

    def test_matched_categories_includes_broken_payload(self) -> None:
        cats = matched_categories("[tool_error] Broken payload (type=empty_payload).")
        assert ToolFailureCategory.broken_payload in cats

    def test_broken_payload_precedes_generic_malformed(self) -> None:
        """The explicit USR signal must not be swallowed by the generic
        'malformed' rule."""
        result = classify_tool_failure(
            "read_file",
            "[tool_error] Broken payload (type=malformed_payload).",
        )
        assert result.category == ToolFailureCategory.broken_payload

    def test_plain_malformed_text_still_unexpected_output(self) -> None:
        """Without the [tool_error] marker, generic malformed text keeps its
        existing category — the new rule must not over-match."""
        result = classify_tool_failure("read_file", "malformed output")
        assert result.category == ToolFailureCategory.unexpected_output
