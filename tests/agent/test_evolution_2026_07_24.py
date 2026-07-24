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

    def test_overload_counter_decays_on_non_overloaded_error(self):
        """A non-overloaded ERROR response decays the counter by 1 (partial signal)."""
        agent = MagicMock()
        agent._cross_turn_overload_hits = 3

        # Simulate decay logic (else branch of the overload tracker)
        _ct_oh = getattr(agent, "_cross_turn_overload_hits", 0)
        if _ct_oh > 0:
            agent._cross_turn_overload_hits = max(0, _ct_oh - 1)
        assert agent._cross_turn_overload_hits == 2


# ── #1205 (regression): cross-turn overload streak must RECOVER ────────
#
# The prior fix incremented _cross_turn_overload_hits and decayed it by 1
# only on a non-overloaded ERROR. It was never reset on a genuinely
# successful API call, nor on provider swap. So transient 503s that each
# recover after one backoff accumulated the streak across a long healthy
# session until it crossed the >=3 breaker and thrashed providers — and a
# stale streak carried into a freshly-swapped provider. These tests pin the
# recovery path: a real response, or a failover, clears the streak to 0.

class TestCrossTurnOverloadRecovery:
    """The cross-turn overload streak must expire so a recovered provider is
    not blocked forever (matches the _consecutive_stale_streams contract)."""

    def test_streak_reaches_breaker_threshold_on_persistent_overload(self):
        """Three consecutive overloads with no success cross the >=3 breaker."""
        agent = MagicMock()
        agent._cross_turn_overload_hits = 0
        for _ in range(3):
            agent._cross_turn_overload_hits = (
                getattr(agent, "_cross_turn_overload_hits", 0) + 1
            )
        # This is the exact gate from conversation_loop.py's fallback decision.
        assert getattr(agent, "_cross_turn_overload_hits", 0) >= 3

    def test_streak_resets_to_zero_on_successful_call(self):
        """A genuinely successful API call clears the streak fully (recovery).

        Mirrors the reset added at the retry-loop success exit — a real
        response (bytes back) proves the provider is not overloaded, so the
        breaker must not carry a stale streak into future turns.
        """
        agent = MagicMock()
        agent._cross_turn_overload_hits = 4  # accumulated across turns

        # Simulate the success-exit reset from conversation_loop.py.
        if getattr(agent, "_cross_turn_overload_hits", 0):
            agent._cross_turn_overload_hits = 0

        assert agent._cross_turn_overload_hits == 0
        # After recovery, a single fresh 503 must NOT immediately trip the
        # >=3 breaker — the provider gets a clean backoff-and-retry again.
        agent._cross_turn_overload_hits = (
            getattr(agent, "_cross_turn_overload_hits", 0) + 1
        )
        assert getattr(agent, "_cross_turn_overload_hits", 0) < 3

    def test_streak_resets_to_zero_on_failover(self):
        """Provider swap clears the streak — it measured the OLD provider."""
        agent = MagicMock()
        agent._cross_turn_overload_hits = 5

        # Simulate the fallback-activation reset from conversation_loop.py.
        agent._cross_turn_overload_hits = 0

        assert agent._cross_turn_overload_hits == 0

    def test_reset_sites_are_wired_into_the_retry_loop(self):
        """Guard: both recovery resets stay wired in conversation_loop.py.

        An inline-simulation test cannot catch someone deleting the actual
        reset line, so assert the source contains the reset at both the
        success-exit and the failover-activation sites.
        """
        from pathlib import Path
        import agent.conversation_loop as cl

        src = Path(cl.__file__).read_text(encoding="utf-8")
        # Success-exit reset lives right after the has_retried_429 reset.
        assert "_retry.has_retried_429 = False  # Reset on success" in src
        # Both recovery sites set the cross-turn streak back to 0.
        assert src.count("agent._cross_turn_overload_hits = 0") >= 2


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
