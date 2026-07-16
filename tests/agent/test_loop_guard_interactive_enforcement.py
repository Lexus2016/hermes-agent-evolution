"""Tests for #1109–#1112: interactive failing-spiral enforcement.

On interactive surfaces (CLI/gateway/messaging), after
``INTERACTIVE_LOOP_GUARD_HARD_STOP_THRESHOLD`` advisory warnings for a GENUINE
failing spiral (``run_warrants_cron_hard_stop`` is True — the tool is failing
or stuck in an identical-call loop), the turn ends as a failure instead of
continuing to nudge. This extends the cron-only enforcement (#624) to
interactive surfaces for genuine spirals, preventing the observed retry spirals
(29 consecutive terminal calls, 17 execute_code, 21 browser_navigate, 8
read_file) from burning context with no progress.

Part 1 — pure-function tests: no agent/import-chain dependencies, fast, fully
isolated.

Part 2 — integration tests are in test_loop_guard_cron_enforcement.py (requires
Python 3.10+ for the ``run_agent`` import chain).
"""

from agent.loop_guard import (
    INTERACTIVE_LOOP_GUARD_HARD_STOP_THRESHOLD,
    run_warrants_cron_hard_stop,
    should_interactive_hard_stop,
)


# ── Message helpers (mirror test_loop_guard.py) ──────────────────────────


def _asst(tool, args="{}", call_id="c"):
    return {
        "role": "assistant",
        "tool_calls": [
            {"id": call_id, "function": {"name": tool, "arguments": args}}
        ],
    }


def _result(content, call_id="c"):
    return {"role": "tool", "tool_call_id": call_id, "content": content}


def _failing_run(tool, n, *, error="error: something failed"):
    """n consecutive single-tool turns, each with a FAILING result."""
    msgs = [{"role": "user", "content": "do the thing"}]
    for i in range(n):
        cid = f"c{i}"
        msgs.append(_asst(tool, call_id=cid))
        msgs.append(_result(error, call_id=cid))
    return msgs


def _identical_ok_run(tool, n):
    """n consecutive single-tool turns with the SAME args and ok results
    — a degenerate identical-call loop that run_warrants_cron_hard_stop
    flags via clause (3)."""
    msgs = [{"role": "user", "content": "do the thing"}]
    for i in range(n):
        cid = f"c{i}"
        msgs.append(_asst(tool, args='{"x": 1}', call_id=cid))
        msgs.append(_result("ok", call_id=cid))
    return msgs


def _distinct_ok_run(tool, n):
    """n consecutive single-tool turns with DISTINCT args and ok results
    — legitimate mono-tool progress (e.g. git add / commit / push burst)."""
    msgs = [{"role": "user", "content": "do the thing"}]
    for i in range(n):
        cid = f"c{i}"
        msgs.append(_asst(tool, args=f'{{"step": {i}}}', call_id=cid))
        msgs.append(_result("ok", call_id=cid))
    return msgs


# ── Tests: should_interactive_hard_stop pure function ────────────────────


class TestShouldInteractiveHardStopPure:
    """Pure-function tests for the interactive hard-stop gate."""

    def test_below_threshold_never_stops(self):
        assert (
            should_interactive_hard_stop(
                "cli",
                INTERACTIVE_LOOP_GUARD_HARD_STOP_THRESHOLD - 1,
                genuine_spiral=True,
            )
            is False
        )

    def test_at_threshold_stops_for_genuine_spiral(self):
        assert (
            should_interactive_hard_stop(
                "cli",
                INTERACTIVE_LOOP_GUARD_HARD_STOP_THRESHOLD,
                genuine_spiral=True,
            )
            is True
        )

    def test_above_threshold_stops(self):
        assert (
            should_interactive_hard_stop(
                "telegram",
                INTERACTIVE_LOOP_GUARD_HARD_STOP_THRESHOLD + 5,
                genuine_spiral=True,
            )
            is True
        )

    def test_never_stops_for_non_genuine_spiral(self):
        """Repetitive-but-successful work: keep nudging, never hard-stop."""
        huge = INTERACTIVE_LOOP_GUARD_HARD_STOP_THRESHOLD + 100
        for platform in ("cli", "gateway", "telegram", "discord", None, ""):
            assert should_interactive_hard_stop(
                platform, huge, genuine_spiral=False
            ) is False, platform

    def test_cron_platform_never_triggers_interactive_path(self):
        """Cron uses its own lower-threshold path; should_interactive_hard_stop
        must defer to that and always return False for cron."""
        assert (
            should_interactive_hard_stop(
                "cron",
                INTERACTIVE_LOOP_GUARD_HARD_STOP_THRESHOLD + 10,
                genuine_spiral=True,
            )
            is False
        )

    def test_all_interactive_platforms_covered(self):
        """Every non-cron platform should be eligible once the threshold is
        met for a genuine spiral."""
        for platform in ("cli", "gateway", "telegram", "discord", "web", None, ""):
            assert should_interactive_hard_stop(
                platform,
                INTERACTIVE_LOOP_GUARD_HARD_STOP_THRESHOLD,
                genuine_spiral=True,
            ) is True, platform

    def test_zero_warnings_never_stops(self):
        assert should_interactive_hard_stop("cli", 0, genuine_spiral=True) is False


# ── Tests: run_warrants_cron_hard_stop classifies correctly ──────────────
# These verify the second gate that the interactive hard-stop depends on —
# confirming that genuine spirals are True and legitimate work is False.


class TestGenuineSpiralClassification:
    """Verify run_warrants_cron_hard_stop (the ``genuine_spiral`` input)
    classifies the right patterns as real spirals vs. legitimate work."""

    def test_failing_terminal_spiral_is_genuine(self):
        """#1109 — terminal failing repeatedly is a genuine spiral."""
        assert run_warrants_cron_hard_stop(
            _failing_run("terminal", 4)
        ) is True

    def test_failing_execute_code_spiral_is_genuine(self):
        """#1110 — execute_code failing repeatedly is a genuine spiral."""
        assert run_warrants_cron_hard_stop(
            _failing_run("execute_code", 4)
        ) is True

    def test_failing_browser_navigate_spiral_is_genuine(self):
        """#1111 — browser_navigate failing repeatedly is a genuine spiral."""
        assert run_warrants_cron_hard_stop(
            _failing_run("browser_navigate", 4)
        ) is True

    def test_failing_read_file_spiral_is_genuine(self):
        """#1112 — read_file failing repeatedly is a genuine spiral."""
        assert run_warrants_cron_hard_stop(
            _failing_run("read_file", 5)
        ) is True

    def test_identical_ok_terminal_loop_is_genuine(self):
        """Degenerate identical-call loop (``echo hi`` x N) is genuine even
        though each call 'succeeded' — run_warrants clause (3)."""
        assert run_warrants_cron_hard_stop(
            _identical_ok_run("terminal", 5)
        ) is True

    def test_distinct_successful_terminal_burst_is_not_genuine(self):
        """Distinct successful calls (real progress: lint -> git add ->
        commit -> push) must NOT be classified as a genuine spiral."""
        assert (
            run_warrants_cron_hard_stop(
                _distinct_ok_run("terminal", 10)
            )
            is False
        )

    def test_distinct_successful_read_file_burst_is_not_genuine(self):
        """Reading several DIFFERENT files in a row is legitimate exploration,
        not a spiral — must NOT be classified as genuine."""
        assert (
            run_warrants_cron_hard_stop(
                _distinct_ok_run("read_file", 10)
            )
            is False
        )
