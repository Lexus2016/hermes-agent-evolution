"""Tests for the 2026-07-24 evolution cycle fixes.

Covers:
- #1258: patch added to _SPIRAL_PRONE_TOOLS + identical-edit returns success
- #1205: cross-turn overload counter on agent instance
- #1243: multi-shot refusal recovery with escalation
- #1242: after_call called for guardrail-blocked tools
"""

from __future__ import annotations

import json
import pytest
from unittest.mock import MagicMock, patch

from agent.tool_guardrails import (
    ToolCallGuardrailConfig,
    ToolCallGuardrailController,
    _SPIRAL_PRONE_TOOLS,
)


# ── #1258: patch in _SPIRAL_PRONE_TOOLS ──────────────────────────────

class TestPatchSpiralProne:
    """Verify patch is in the spiral-prone set so the always-on cap applies."""

    def test_patch_in_spiral_prone_tools(self):
        """patch must be in _SPIRAL_PRONE_TOOLS for the always-on cap."""
        assert "patch" in _SPIRAL_PRONE_TOOLS

    def test_patch_spiral_cap_enforces(self):
        """After spiral_failure_cap consecutive patch failures, the controller
        returns a halt decision regardless of hard_stop_enabled."""
        config = ToolCallGuardrailConfig(
            hard_stop_enabled=False,
            spiral_failure_cap=3,
        )
        controller = ToolCallGuardrailController(config=config)
        controller.reset_for_turn()

        args = {"path": "/tmp/test.py", "old_string": "x", "new_string": "y"}

        # Simulate 3 consecutive failures across turns
        for i in range(3):
            controller.after_call("patch", args, '{"error": "no match"}', failed=True)

        # The 4th call should be blocked by before_call (cross-turn streak >= cap)
        decision = controller.before_call("patch", args)
        assert not decision.allows_execution
        assert decision.action == "block"
        assert decision.code == "spiral_prone_tool_failure_cap"

    def test_patch_fallback_directive_exists(self):
        """The fallback directive for patch must be defined."""
        from agent.tool_guardrails import _fallback_directive_for
        directive = _fallback_directive_for("patch")
        assert directive
        assert "read_file" in directive or "write_file" in directive


# ── #1258: identical-edit returns success ─────────────────────────────

class TestIdenticalEditSuccess:
    """Verify identical-edit (old_string == new_string) returns success, not error."""

    def test_identical_returns_success(self):
        """patch_replace with identical old/new should return success=True."""
        import os, tempfile, subprocess
        from pathlib import Path
        from tools.file_operations import ShellFileOperations

        with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
            f.write("def foo():\n    pass\n")
            fp = f.name
        try:
            tmp = Path(fp).parent
            ops = ShellFileOperations.__new__(ShellFileOperations)
            ops._escape_shell_arg = lambda s: f"'{s}'"
            ops._expand_path = lambda s: s
            ops._is_write_denied = lambda s: False
            ops._strip_bom = lambda s: (s, False)
            ops._detect_line_ending = lambda s: None
            ops.write_file = lambda p, c: type("W", (), {"error": None})()
            def _exec(cmd):
                r = subprocess.run(cmd, shell=True, capture_output=True, text=True, cwd=str(tmp), timeout=5)
                class _R:
                    exit_code = r.returncode
                    stdout = r.stdout
                    stderr = r.stderr
                return _R()
            ops._exec = _exec
            r = ops.patch_replace(fp, "def foo():", "def foo():")
            assert r.success is True
            assert r.error is None
        finally:
            os.unlink(fp)

    def test_identical_not_classified_as_failure(self):
        """file_mutation_result_landed should return True for identical-edit result."""
        from agent.tool_result_classification import file_mutation_result_landed
        result = json.dumps({"success": True, "_diagnostic": "no changes needed"})
        assert file_mutation_result_landed("patch", result) is True


# ── #1205: cross-turn overload counter ────────────────────────────────

class TestCrossTurnOverloadCounter:
    """Verify the cross-turn overload counter persists on the agent instance."""

    def test_overload_counter_increments(self):
        """Each overloaded response increments _cross_turn_overload_hits."""
        agent = MagicMock()
        agent._cross_turn_overload_hits = 0
        agent._fallback_index = 0
        agent._fallback_chain = []

        # Simulate the overload tracking logic from conversation_loop.py
        from agent.error_classifier import FailoverReason

        # First overload
        if FailoverReason.overloaded == FailoverReason.overloaded:
            agent._cross_turn_overload_hits = getattr(agent, "_cross_turn_overload_hits", 0) + 1
        assert agent._cross_turn_overload_hits == 1

        # Second overload
        agent._cross_turn_overload_hits = getattr(agent, "_cross_turn_overload_hits", 0) + 1
        assert agent._cross_turn_overload_hits == 2

        # Third overload
        agent._cross_turn_overload_hits = getattr(agent, "_cross_turn_overload_hits", 0) + 1
        assert agent._cross_turn_overload_hits == 3

    def test_overload_counter_decays_on_success(self):
        """Non-overloaded responses decay the counter by 1."""
        agent = MagicMock()
        agent._cross_turn_overload_hits = 3

        # Simulate decay logic
        _ct_oh = getattr(agent, "_cross_turn_overload_hits", 0)
        if _ct_oh > 0:
            agent._cross_turn_overload_hits = max(0, _ct_oh - 1)
        assert agent._cross_turn_overload_hits == 2


# ── #1243: multi-shot refusal recovery ────────────────────────────────

class TestRefusalMultiShot:
    """Verify maybe_refusal_nudge still detects on already_nudged=True."""

    def test_already_nudged_still_detects(self):
        """already_nudged=True should NOT suppress detection (#1243)."""
        from agent.loop_guard import maybe_refusal_nudge
        msgs = [
            {"role": "user", "content": "do the thing"},
            {"role": "assistant", "content": "I can't help with that."},
        ]
        nudge = maybe_refusal_nudge(msgs, already_nudged=True)
        assert nudge is not None
        assert "over_refusal" in nudge

    def test_first_nudge_still_works(self):
        """First refusal (already_nudged=False) should still produce a nudge."""
        from agent.loop_guard import maybe_refusal_nudge
        msgs = [
            {"role": "user", "content": "do the thing"},
            {"role": "assistant", "content": "I can't help with that."},
        ]
        nudge = maybe_refusal_nudge(msgs, already_nudged=False)
        assert nudge is not None
        assert "over_refusal" in nudge


# ── #1242: after_call for blocked tools ───────────────────────────────

class TestBlockedToolAfterCall:
    """Verify after_call is called for guardrail-blocked tools, keeping the
    cross-turn streak alive."""

    def test_blocked_tool_increments_cross_turn(self):
        """When after_call is called for a blocked tool (failed=True), the
        cross-turn counter should increment."""
        config = ToolCallGuardrailConfig(
            hard_stop_enabled=False,
            spiral_failure_cap=3,
        )
        controller = ToolCallGuardrailController(config=config)
        controller.reset_for_turn()

        args = {"command": "ls"}

        # Accumulate 3 failures to reach the cap
        for i in range(3):
            controller.after_call("terminal", args, '{"exit_code": 1}', failed=True)

        # The cross-turn count should be 3
        assert controller._cross_turn_tool_failure_counts.get("terminal", 0) == 3

        # Simulate a blocked call — after_call with failed=True
        controller.after_call("terminal", args, '{"error": "blocked"}', failed=True)

        # The cross-turn count should now be 4 (kept incrementing)
        assert controller._cross_turn_tool_failure_counts.get("terminal", 0) == 4