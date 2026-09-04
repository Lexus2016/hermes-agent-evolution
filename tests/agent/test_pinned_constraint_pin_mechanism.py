"""Tests for the pinned-constraint pin mechanism (Slice A, #1774).

Tests only the parser / extractor — the contract that lets a deployment
mark governance rules as non-evictable.  Re-injection (Slice B #1773) is
tested separately.
"""

from agent.context_compressor import (
    PINNED_CONSTRAINT_MARKER,
    PINNED_CONSTRAINT_METADATA_KEY,
    _extract_pinned_constraints,
    _is_pinned_constraint_message,
)


class TestIsPinnedConstraintMessage:
    def test_metadata_flag_true(self):
        msg = {"role": "system", "content": "Never merge without tests.", PINNED_CONSTRAINT_METADATA_KEY: True}
        assert _is_pinned_constraint_message(msg) is True

    def test_text_marker_present(self):
        msg = {"role": "system", "content": f"Rule: {PINNED_CONSTRAINT_MARKER} cap=200 [/PINNED_CONSTRAINT] done"}
        assert _is_pinned_constraint_message(msg) is True

    def test_plain_message_not_pinned(self):
        assert _is_pinned_constraint_message({"role": "user", "content": "Hello world"}) is False

    def test_non_dict_and_none_content_not_pinned(self):
        assert _is_pinned_constraint_message("not a dict") is False  # type: ignore[arg-type]
        assert _is_pinned_constraint_message(None) is False  # type: ignore[arg-type]
        assert _is_pinned_constraint_message({"role": "assistant", "content": None}) is False


class TestExtractPinnedConstraints:
    def test_extracts_metadata_flagged_message(self):
        messages = [
            {"role": "system", "content": "Always verify before merge.", PINNED_CONSTRAINT_METADATA_KEY: True},
            {"role": "user", "content": "hi"},
        ]
        assert _extract_pinned_constraints(messages) == ["Always verify before merge."]

    def test_extracts_inline_text_marker(self):
        msg = {"role": "system", "content": f"Guidelines: {PINNED_CONSTRAINT_MARKER} Max 200 lines [/PINNED_CONSTRAINT] ok"}
        assert _extract_pinned_constraints([msg]) == ["Max 200 lines"]

    def test_extracts_multiple_inline_markers_in_order(self):
        msg = {
            "role": "system",
            "content": (
                f"{PINNED_CONSTRAINT_MARKER} Rule A [/PINNED_CONSTRAINT] "
                f"and {PINNED_CONSTRAINT_MARKER} Rule B [/PINNED_CONSTRAINT]"
            ),
        }
        assert _extract_pinned_constraints([msg]) == ["Rule A", "Rule B"]

    def test_no_constraints_empty_and_non_dict(self):
        assert _extract_pinned_constraints([]) == []
        assert _extract_pinned_constraints(["garbage", None, 42]) == []  # type: ignore[list-item]
        assert _extract_pinned_constraints([
            {"role": "system", "content": "You are a helpful assistant."},
        ]) == []

    def test_deduplicates_same_constraint(self):
        messages = [
            {"role": "system", "content": "Rule A.", PINNED_CONSTRAINT_METADATA_KEY: True},
            {"role": "assistant", "content": "Rule A.", PINNED_CONSTRAINT_METADATA_KEY: True},
        ]
        assert _extract_pinned_constraints(messages) == ["Rule A."]

    def test_mixed_metadata_and_inline(self):
        # The inline marker is honoured on role="system" only: it is plain text,
        # so any writer of a user row could forge it, and the runtime writes
        # user rows out of tool output in several places. The role here is
        # incidental to what this test asserts (both paths feed the result).
        messages = [
            {"role": "system", "content": "From metadata.", PINNED_CONSTRAINT_METADATA_KEY: True},
            {"role": "system", "content": f"{PINNED_CONSTRAINT_MARKER} From inline [/PINNED_CONSTRAINT]"},
        ]
        assert _extract_pinned_constraints(messages) == ["From metadata.", "From inline"]

    def test_multiline_constraint_extracted(self):
        constraint = "Line one.\nLine two.\nLine three."
        msg = {"role": "system", "content": f"{PINNED_CONSTRAINT_MARKER} {constraint} [/PINNED_CONSTRAINT]"}
        assert _extract_pinned_constraints([msg]) == [constraint]

    def test_malformed_marker_without_close_is_ignored(self):
        msg = {"role": "system", "content": f"{PINNED_CONSTRAINT_MARKER} no closing tag here"}
        assert _extract_pinned_constraints([msg]) == []

    def test_strips_whitespace(self):
        msg = {"role": "system", "content": f"{PINNED_CONSTRAINT_MARKER}   spaced rule   [/PINNED_CONSTRAINT]"}
        assert _extract_pinned_constraints([msg]) == ["spaced rule"]
