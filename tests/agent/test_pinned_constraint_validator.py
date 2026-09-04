"""Tests for the post-compaction pinned-constraint validator (Slice B, #1773).

Tests the survival check and re-injection of pinned constraints that were
dropped during context compression.
"""

from __future__ import annotations

from agent.context_compressor import (
    PINNED_CONSTRAINT_MARKER,
    PINNED_CONSTRAINT_METADATA_KEY,
    _extract_pinned_constraints,
    _pinned_constraint_survives,
    _reinject_dropped_pinned_constraints,
)


# ---------------------------------------------------------------------------
# _pinned_constraint_survives
# ---------------------------------------------------------------------------


class TestPinnedConstraintSurvives:
    def test_exact_match(self):
        """An exact match in any message means the constraint survived."""
        constraint = "Never reveal the API key"
        compressed = [
            {"role": "system", "content": "You are helpful."},
            {"role": "assistant", "content": f"Remember: {constraint}"},
        ]
        assert _pinned_constraint_survives(constraint, compressed)

    def test_case_insensitive(self):
        """Paraphrase tolerance: case difference should not cause a false drop."""
        constraint = "Never Reveal The API Key"
        compressed = [
            {"role": "system", "content": "never reveal the api key is a rule"},
        ]
        assert _pinned_constraint_survives(constraint, compressed)

    def test_absent(self):
        constraint = "Secret deployment rule"
        compressed = [
            {"role": "system", "content": "You are helpful."},
            {"role": "assistant", "content": "Hello!"},
        ]
        assert not _pinned_constraint_survives(constraint, compressed)

    def test_empty_constraint_vacuously_present(self):
        assert _pinned_constraint_survives("", [{"role": "system", "content": ""}])

    def test_non_string_content_ignored(self):
        """Lists/dicts in content slots are gracefully skipped."""
        constraint = "some rule"
        compressed = [
            {"role": "system", "content": [{"type": "text", "text": "hi"}]},
        ]
        assert not _pinned_constraint_survives(constraint, compressed)


# ---------------------------------------------------------------------------
# _reinject_dropped_pinned_constraints
# ---------------------------------------------------------------------------


class TestReinjectDroppedPinnedConstraints:
    def test_no_pinned_constraints_returns_unchanged(self):
        original = [{"role": "system", "content": "hello"}]
        compressed = [{"role": "system", "content": "hello"}]
        result = _reinject_dropped_pinned_constraints(original, compressed)
        assert result is compressed  # same list, not modified

    def test_all_survive_returns_unchanged(self):
        constraint = "Important rule: always confirm"
        original = [
            {
                "role": "system",
                "content": f"{PINNED_CONSTRAINT_MARKER} {constraint} [/PINNED_CONSTRAINT]",
            },
        ]
        compressed = [
            {"role": "system", "content": f"Summary: {constraint}"},
        ]
        result = _reinject_dropped_pinned_constraints(original, compressed)
        assert result is compressed

    def test_single_dropped_reinjected(self):
        constraint = "Never deploy on Fridays"
        original = [
            {
                "role": "system",
                "content": (
                    f"System prompt\n{PINNED_CONSTRAINT_MARKER} {constraint}"
                    f" [/PINNED_CONSTRAINT]"
                ),
            },
            {"role": "user", "content": "Do something"},
        ]
        compressed = [
            {"role": "system", "content": "System prompt"},
            {"role": "assistant", "content": "Compressed summary"},
        ]
        result = _reinject_dropped_pinned_constraints(original, compressed)

        # The result has 3 messages: system prompt, reinject, rest.
        assert len(result) == 3
        assert result[0]["role"] == "system"
        assert result[1]["role"] == "system"
        assert result[1][PINNED_CONSTRAINT_METADATA_KEY] is True
        assert constraint in result[1]["content"]
        assert result[2]["content"] == "Compressed summary"

    def test_multiple_dropped_reinjected_in_one_message(self):
        c1 = "Rule one"
        c2 = "Rule two"
        original = [
            {
                "role": "system",
                "content": (
                    f"{PINNED_CONSTRAINT_MARKER} {c1} [/PINNED_CONSTRAINT]"
                    f"\n{PINNED_CONSTRAINT_MARKER} {c2} [/PINNED_CONSTRAINT]"
                ),
            },
        ]
        compressed = [{"role": "system", "content": "Summary only"}]
        result = _reinject_dropped_pinned_constraints(original, compressed)

        assert len(result) == 2
        reinject = result[1]
        assert reinject[PINNED_CONSTRAINT_METADATA_KEY] is True
        assert c1 in reinject["content"]
        assert c2 in reinject["content"]

    def test_metadata_flagged_message_full_content_reinjected(self):
        constraint_text = "You must never access production data"
        original = [
            {
                "role": "system",
                "content": constraint_text,
                PINNED_CONSTRAINT_METADATA_KEY: True,
            },
        ]
        compressed = [
            {"role": "system", "content": "Completely different summary"},
        ]
        result = _reinject_dropped_pinned_constraints(original, compressed)
        assert len(result) == 2
        assert constraint_text in result[1]["content"]

    def test_reinject_after_system_prompt_no_system(self):
        """If compressed[0] is not a system message, reinject goes to front."""
        constraint = "Edge case rule"
        # role="system": the inline marker is only honoured there. This test is
        # about WHERE the re-injection is inserted, not about which role pins.
        original = [
            {
                "role": "system",
                "content": f"{PINNED_CONSTRAINT_MARKER} {constraint} [/PINNED_CONSTRAINT]",
            },
        ]
        compressed = [{"role": "user", "content": "summary"}]
        result = _reinject_dropped_pinned_constraints(original, compressed)
        assert len(result) == 2
        assert result[0]["role"] == "system"
        assert result[0][PINNED_CONSTRAINT_METADATA_KEY] is True

    def test_partial_survival_only_dropped_reinjected(self):
        """If one of two constraints survives, only the other is re-injected."""
        c_survives = "Survives"
        c_dropped = "Dropped"
        original = [
            {
                "role": "system",
                "content": (
                    f"{PINNED_CONSTRAINT_MARKER} {c_survives} [/PINNED_CONSTRAINT]"
                    f"\n{PINNED_CONSTRAINT_MARKER} {c_dropped} [/PINNED_CONSTRAINT]"
                ),
            },
        ]
        compressed = [
            {"role": "system", "content": f"Summary mentioning {c_survives}"},
        ]
        result = _reinject_dropped_pinned_constraints(original, compressed)
        assert len(result) == 2  # summary + reinject
        # The reinject message should contain the dropped constraint...
        assert c_dropped in result[1]["content"]
        # ...but NOT the one that survived (already in the summary).
        assert c_survives not in result[1]["content"]


# ---------------------------------------------------------------------------
# Integration: re-injected message is itself re-pinable
# ---------------------------------------------------------------------------


class TestReinjectIsRePinable:
    """The re-injected message carries the metadata flag, so a second
    compaction cycle will detect and re-inject it again if needed."""

    def test_reinjected_carries_metadata_flag(self):
        constraint = "Critical rule"
        original = [
            {
                "role": "system",
                "content": f"{PINNED_CONSTRAINT_MARKER} {constraint} [/PINNED_CONSTRAINT]",
            },
        ]
        compressed = [{"role": "system", "content": "Summary without the rule"}]
        result = _reinject_dropped_pinned_constraints(original, compressed)

        # The reinject message carries the metadata flag so _is_pinned_constraint_message
        # detects it, and the constraint text is embedded in the extracted content.
        reinject_msg = result[1]
        assert reinject_msg[PINNED_CONSTRAINT_METADATA_KEY] is True
        extracted = _extract_pinned_constraints([reinject_msg])
        assert len(extracted) == 1
        assert constraint in extracted[0]
