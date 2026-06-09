#!/usr/bin/env python3
"""Tests for attention_reset module."""

import pytest

from tools.attention_reset import (
    ResetContext,
    ResetResult,
    AttentionReset,
    attention_reset_hook,
)


@pytest.fixture
def reset():
    """Create a fresh AttentionReset instance for each test."""
    return AttentionReset()


class TestResetContext:
    """Test ResetContext dataclass."""

    def test_context_creation(self):
        """Test ResetContext can be created."""
        context = ResetContext(
            trigger_reason="failed_attempts",
            failed_attempts=3,
            task_description="Debug test failure",
            current_hypothesis="Bug in function",
        )
        assert context.trigger_reason == "failed_attempts"
        assert context.failed_attempts == 3
        assert context.task_description == "Debug test failure"
        assert context.current_hypothesis == "Bug in function"
        assert context.reset_count == 0


class TestResetResult:
    """Test ResetResult dataclass."""

    def test_result_creation(self):
        """Test ResetResult can be created."""
        context = ResetContext(trigger_reason="test")
        result = ResetResult(
            generated_string="abc123xyz",
            digit_sum=6,
            position=6,
            selected_char="x",
            announcement="reset: abc123xyz (digit-sum 6, pos 6): x",
            context=context,
        )
        assert result.generated_string == "abc123xyz"
        assert result.digit_sum == 6
        assert result.position == 6
        assert result.selected_char == "x"
        assert str(result) == result.announcement
        assert result.timestamp > 0


class TestAttentionReset:
    """Test AttentionReset main class."""

    def test_initialization(self):
        """Test AttentionReset initialization."""
        reset = AttentionReset(failed_attempt_threshold=3, max_resets_per_task=10)
        assert reset.failed_attempt_threshold == 3
        assert reset.max_resets_per_task == 10
        assert reset._failed_attempts == 0
        assert reset._reset_count == 0

    def test_initialization_defaults(self):
        """Test AttentionReset initialization with defaults."""
        reset = AttentionReset()
        assert reset.failed_attempt_threshold == 2
        assert reset.max_resets_per_task == 5
        assert reset._failed_attempts == 0
        assert reset._reset_count == 0

    def test_reset_state(self):
        """Test resetting state."""
        reset = AttentionReset()
        reset._failed_attempts = 5
        reset._reset_count = 3
        reset._last_trigger_reason = "test"
        reset.reset_state()
        assert reset._failed_attempts == 0
        assert reset._reset_count == 0
        assert reset._last_trigger_reason is None

    def test_record_failure(self):
        """Test recording a failed attempt."""
        reset = AttentionReset()
        reset.record_failure("hypothesis_failed", "Bug is in parser", "Parse error")
        assert reset._failed_attempts == 1
        assert reset._last_trigger_reason == "hypothesis_failed"

        reset.record_failure("another_failure")
        assert reset._failed_attempts == 2

    def test_record_success(self):
        """Test recording success resets failure counter."""
        reset = AttentionReset()
        reset._failed_attempts = 3
        reset.record_success()
        assert reset._failed_attempts == 0
        assert reset._last_trigger_reason is None

    def test_should_trigger_reset_on_failures(self, reset):
        """Test reset triggers after threshold failures."""
        assert not reset.should_trigger_reset()

        reset._failed_attempts = 2
        assert reset.should_trigger_reset()

    def test_should_trigger_reset_before_irreversible(self, reset):
        """Test reset triggers before irreversible actions."""
        assert reset.should_trigger_reset(before_irreversible=True)

    def test_should_trigger_reset_overconfident(self, reset):
        """Test reset triggers on overconfidence without check."""
        assert reset.should_trigger_reset(overconfident_without_check=True)

    def test_should_trigger_reset_continuing_reasoning(self, reset):
        """Test reset triggers when continuing prior reasoning."""
        assert reset.should_trigger_reset(continuing_prior_reasoning=True)

    def test_should_not_trigger_reset(self, reset):
        """Test reset does not trigger without conditions."""
        reset._failed_attempts = 1
        assert not reset.should_trigger_reset()

    def test_should_not_trigger_reset_at_max(self, reset):
        """Test reset does not trigger after max resets reached."""
        reset._reset_count = 5
        reset._failed_attempts = 10
        assert not reset.should_trigger_reset()

    def test_trigger_reset_basic(self, reset):
        """Test basic reset execution."""
        result = reset.trigger_reset()

        assert isinstance(result, ResetResult)
        assert len(result.generated_string) == 10
        assert result.digit_sum >= 0
        assert 0 <= result.position <= 9
        assert len(result.selected_char) == 1
        assert "reset:" in result.announcement
        assert result.context.reset_count == 1

    def test_trigger_reset_increments_count(self, reset):
        """Test reset increments internal counter."""
        reset.trigger_reset()
        assert reset._reset_count == 1

        reset.trigger_reset()
        assert reset._reset_count == 2

    def test_trigger_reset_with_seed(self, reset):
        """Test reset with seed produces deterministic results."""
        result1 = reset.trigger_reset(seed_from_output="test output")
        result2 = reset.trigger_reset(seed_from_output="test output")

        # Same input should produce same result (given same reset count)
        # But since reset count changes, results will differ
        assert result1.generated_string != result2.generated_string

    def test_trigger_reset_resets_failures(self, reset):
        """Test reset clears failed attempts counter."""
        reset._failed_attempts = 5
        result = reset.trigger_reset()
        assert reset._failed_attempts == 0

    def test_trigger_reset_digit_calculation(self, reset):
        """Test digit sum calculation."""
        result = reset.trigger_reset(seed_from_output="abc123")

        # Calculate expected digit sum
        digit_sum = sum(int(c) for c in result.generated_string if c.isdigit())
        assert result.digit_sum == digit_sum

    def test_trigger_reset_position_calculation(self, reset):
        """Test position is calculated from digit sum."""
        result = reset.trigger_reset()
        expected_position = result.digit_sum % 10
        assert result.position == expected_position

    def test_trigger_reset_selected_char(self, reset):
        """Test selected character matches position."""
        result = reset.trigger_reset()
        assert result.selected_char == result.generated_string[result.position]

    def test_get_reset_prompt(self, reset):
        """Test getting reset prompt."""
        result = reset.trigger_reset()
        prompt = reset.get_reset_prompt(result)

        assert "attention reset" in prompt.lower()
        assert result.announcement in prompt
        assert "Re-read the original task" in prompt

    def test_get_reset_prompt_no_result(self, reset):
        """Test getting reset prompt without result."""
        prompt = reset.get_reset_prompt()
        assert "attention reset" in prompt.lower()

    def test_should_abort_task(self, reset):
        """Test task abort after too many resets."""
        assert not reset.should_abort_task()

        reset._reset_count = 5
        assert reset.should_abort_task()

    def test_get_state_summary(self, reset):
        """Test getting state summary."""
        reset._failed_attempts = 3
        reset._reset_count = 2
        reset._last_trigger_reason = "test_failure"

        summary = reset.get_state_summary()

        assert summary["failed_attempts"] == 3
        assert summary["reset_count"] == 2
        assert summary["last_trigger_reason"] == "test_failure"
        assert summary["threshold"] == 2
        assert summary["max_resets"] == 5


class TestAttentionResetHook:
    """Test convenience hook function."""

    def test_hook_triggers_on_failures(self):
        """Test hook triggers after enough failures."""
        announcement = attention_reset_hook(
            failed_attempts=2,
            hypothesis="Bug in parser",
            task_description="Debug test",
        )
        assert announcement is not None
        assert "reset:" in announcement

    def test_hook_no_trigger_single_failure(self):
        """Test hook does not trigger on single failure."""
        announcement = attention_reset_hook(failed_attempts=1)
        assert announcement is None

    def test_hook_triggers_before_irreversible(self):
        """Test hook triggers before irreversible action."""
        announcement = attention_reset_hook(
            failed_attempts=1,
            before_irreversible=True,
        )
        assert announcement is not None

    def test_hook_context_creation(self):
        """Test hook creates proper context."""
        announcement = attention_reset_hook(
            failed_attempts=3,
            hypothesis="Test hypothesis",
            task_description="Test task",
        )
        assert announcement is not None


@pytest.mark.integration
class TestAttentionResetIntegration:
    """Integration tests for attention reset mechanism."""

    def test_full_reset_workflow(self, reset):
        """Test complete workflow from failure to reset."""
        # Record failures
        reset.record_failure("hypothesis_1_failed", "Bug in X")
        reset.record_failure("hypothesis_2_failed", "Bug in Y")

        # Should trigger
        assert reset.should_trigger_reset()

        # Perform reset
        result = reset.trigger_reset(
            seed_from_output="recent agent output",
            context=ResetContext(
                trigger_reason="failed_attempts",
                task_description="Debug test",
                current_hypothesis="Bug in parser",
            ),
        )

        # Verify result
        assert isinstance(result, ResetResult)
        assert result.context.failed_attempts == 2

        # Failures should be cleared
        assert reset._failed_attempts == 0

    def test_max_resets_workflow(self, reset):
        """Test behavior when max resets is reached."""
        # Perform max resets
        for _ in range(5):
            reset._failed_attempts = 2
            assert reset.should_trigger_reset()
            reset.trigger_reset()

        # Next should not trigger
        reset._failed_attempts = 2
        assert not reset.should_trigger_reset()

        # Should recommend abort
        assert reset.should_abort_task()

    def test_reset_with_custom_thresholds(self):
        """Test reset with custom thresholds."""
        reset = AttentionReset(failed_attempt_threshold=5, max_resets_per_task=3)

        # Should not trigger until threshold
        reset._failed_attempts = 4
        assert not reset.should_trigger_reset()

        reset._failed_attempts = 5
        assert reset.should_trigger_reset()

    def test_interleaved_success_and_failure(self, reset):
        """Test behavior with interleaved success and failure."""
        reset.record_failure("fail_1")
        reset.record_failure("fail_2")
        assert reset.should_trigger_reset()

        # Perform reset
        reset.trigger_reset()

        # Now record success - should clear reset count for new task
        reset.record_success()

        # Record new failures - should still be below threshold
        reset.record_failure("fail_3")
        assert not reset.should_trigger_reset()

        reset.record_failure("fail_4")
        assert reset.should_trigger_reset()
