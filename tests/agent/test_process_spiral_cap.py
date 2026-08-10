"""Tests for the process tool spiral cap fix (#2241).

The process tool returns JSON results where the failure signal is NOT a
non-zero ``exit_code`` — it's a ``status`` field (``not_found``, ``error``,
``already_exited``) or a ``success: False`` flag.  The original #1839 fix
only checked ``exit_code``, so these other failure patterns were swallowed
by an early ``return False`` and the cross-turn streak counter never
accumulated, allowing the spiral cap to be bypassed (regression to 18-deep).

These tests verify the expanded classification and that the cap fires.
"""

import json

import pytest

from agent.tool_guardrails import (
    ToolCallGuardrailConfig,
    ToolCallGuardrailController,
    classify_tool_failure,
)


class TestProcessFailureClassification:
    """Process results that lack exit_code but signal failure are now caught."""

    def test_process_nonzero_exit_is_failure(self):
        result = json.dumps({"exit_code": 1, "output": "error output"})
        failed, label = classify_tool_failure("process", result)
        assert failed is True
        assert "exit 1" in label

    def test_process_not_found_is_failure(self):
        result = json.dumps({"status": "not_found", "error": "No process with ID s123"})
        failed, label = classify_tool_failure("process", result)
        assert failed is True
        assert "not_found" in label

    def test_process_error_status_is_failure(self):
        result = json.dumps({"status": "error", "error": "Connection reset by peer"})
        failed, _ = classify_tool_failure("process", result)
        assert failed is True

    def test_process_already_exited_is_failure(self):
        result = json.dumps({
            "status": "already_exited",
            "error": "Process has already finished",
        })
        failed, _ = classify_tool_failure("process", result)
        assert failed is True

    def test_process_success_false_is_failure(self):
        result = json.dumps({"success": False, "error": "write failed: stream closed"})
        failed, _ = classify_tool_failure("process", result)
        assert failed is True

    def test_process_error_without_output_is_failure(self):
        """A process result with 'error' but no 'output' is a failure."""
        result = json.dumps({"error": "stdin not available"})
        failed, _ = classify_tool_failure("process", result)
        assert failed is True

    def test_process_success_exit_zero_not_failure(self):
        result = json.dumps({"exit_code": 0, "output": "all good"})
        failed, _ = classify_tool_failure("process", result)
        assert failed is False

    def test_process_poll_with_output_not_failure(self):
        """A successful poll that has output should NOT be classified as a
        failure even if it carries a status field."""
        result = json.dumps({
            "status": "running",
            "output": "partial data...",
            "exit_code": None,
        })
        failed, _ = classify_tool_failure("process", result)
        assert failed is False


class TestProcessSpiralCapFires:
    """The spiral cap must fire after enough consecutive process failures."""

    def test_cap_fires_after_5_cross_turn_failures(self):
        """Simulating 5 consecutive process failures (one per API turn),
        the 6th call should be blocked by the spiral_failure_cap."""
        controller = ToolCallGuardrailController()
        failing_result = json.dumps({
            "status": "not_found",
            "error": "No process with ID s1",
        })
        for _ in range(5):
            controller.after_call(
                "process", {"action": "poll", "session_id": "s1"}, failing_result
            )
            controller.reset_for_turn()

        decision = controller.before_call(
            "process", {"action": "poll", "session_id": "s1"}
        )
        assert decision.action in ("block", "halt")
        assert decision.code in (
            "spiral_prone_tool_failure_cap",
            "session_hard_stop",
        )

    def test_cap_does_not_fire_on_successes(self):
        """A run of successful process polls should NOT trigger the cap."""
        controller = ToolCallGuardrailController()
        ok_result = json.dumps({"exit_code": 0, "output": "running..."})
        for _ in range(10):
            controller.after_call(
                "process", {"action": "poll", "session_id": "s1"}, ok_result
            )
            controller.reset_for_turn()

        decision = controller.before_call(
            "process", {"action": "poll", "session_id": "s1"}
        )
        assert decision.action != "block"

    def test_18_deep_regression_bounded(self):
        """Regression test: 18 consecutive process failures should be bounded
        by the cap (default 5), not allowed to reach 18."""
        controller = ToolCallGuardrailController()
        failing_result = json.dumps({"status": "error", "error": "connection refused"})
        blocked_at = None
        for i in range(18):
            decision = controller.before_call(
                "process", {"action": "poll", "session_id": "s1"}
            )
            if decision.action == "block":
                blocked_at = i
                break
            controller.after_call(
                "process",
                {"action": "poll", "session_id": "s1"},
                failing_result,
            )
            controller.reset_for_turn()

        assert blocked_at is not None, "Cap never fired after 18 failures"
        assert blocked_at <= 5, f"Cap fired too late at {blocked_at}, expected <= 5"
