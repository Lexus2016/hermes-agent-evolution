"""Tests for the post-compaction pinned-constraint validator (Slice B, #1773).

Tests _pinned_constraint_survives, _reinject_dropped_pinned_constraints, and
the integration call site inside compress()'s terminal post-processing.
"""

from agent.context_compressor import (
    PINNED_CONSTRAINT_METADATA_KEY,
    _extract_pinned_constraints,
    _pinned_constraint_survives,
    _reinject_dropped_pinned_constraints,
)


class TestPinnedConstraintSurvives:
    def test_survives_when_present(self):
        compressed = [{"role": "system", "content": "Always merge with green CI."}]
        assert _pinned_constraint_survives("Always merge with green CI.", compressed) is True

    def test_dropped_when_absent(self):
        compressed = [{"role": "user", "content": "Hello"}]
        assert _pinned_constraint_survives("Max 200 lines per PR", compressed) is False

    def test_case_insensitive(self):
        compressed = [{"role": "system", "content": "NEVER SKIP TESTS."}]
        assert _pinned_constraint_survives("never skip tests.", compressed) is True

    def test_empty_constraint_treated_as_survived(self):
        assert _pinned_constraint_survives("", []) is True

    def test_partial_match_counts_as_present(self):
        compressed = [{"role": "system", "content": "The merge cap is strictly 200 lines."}]
        assert _pinned_constraint_survives("merge cap", compressed) is True


class TestReinjectDroppedPinnedConstraints:
    def test_no_constraints_is_noop(self):
        pre = [{"role": "user", "content": "hi"}]
        compressed = [{"role": "system", "content": "sys"}, {"role": "user", "content": "hi"}]
        result = _reinject_dropped_pinned_constraints(pre, compressed)
        assert result == compressed

    def test_all_survive_is_noop(self):
        pre = [
            {"role": "system", "content": "Rule A", PINNED_CONSTRAINT_METADATA_KEY: True},
            {"role": "system", "content": "Rule B", PINNED_CONSTRAINT_METADATA_KEY: True},
        ]
        compressed = [
            {"role": "system", "content": "Rule A and Rule B apply."},
        ]
        result = _reinject_dropped_pinned_constraints(pre, list(compressed))
        assert len(result) == len(compressed)

    def test_reinjects_single_dropped_constraint(self):
        pre = [{"role": "system", "content": "Max 200 lines.", PINNED_CONSTRAINT_METADATA_KEY: True}]
        compressed = [
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": "hi"},
        ]
        result = _reinject_dropped_pinned_constraints(pre, list(compressed))
        assert len(result) == 3
        reinjected = result[1]
        assert reinjected["role"] == "system"
        assert reinjected[PINNED_CONSTRAINT_METADATA_KEY] is True
        assert "Max 200 lines." in reinjected["content"]

    def test_reinjects_multiple_dropped_in_order(self):
        pre = [
            {"role": "system", "content": "Rule A", PINNED_CONSTRAINT_METADATA_KEY: True},
            {"role": "system", "content": "Rule B", PINNED_CONSTRAINT_METADATA_KEY: True},
        ]
        compressed = [{"role": "system", "content": "sys"}, {"role": "user", "content": "hi"}]
        result = _reinject_dropped_pinned_constraints(pre, list(compressed))
        assert len(result) == 4
        assert "Rule A" in result[1]["content"]
        assert "Rule B" in result[2]["content"]

    def test_reinject_after_system_prompt(self):
        pre = [{"role": "system", "content": "Critical rule", PINNED_CONSTRAINT_METADATA_KEY: True}]
        compressed = [
            {"role": "system", "content": "Main system prompt"},
            {"role": "user", "content": "msg"},
        ]
        result = _reinject_dropped_pinned_constraints(pre, list(compressed))
        assert result[0]["content"] == "Main system prompt"
        assert result[1]["role"] == "system"
        assert "Critical rule" in result[1]["content"]
