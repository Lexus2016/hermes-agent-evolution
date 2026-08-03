"""Tests for subagent task-spec re-grounding (#1578).

The re-grounding nudge re-injects the original delegated goal as a user
message every ``_subagent_reground_interval`` iterations, but only for
``platform == "subagent"``.  Interactive platforms (cli, gateway, etc.)
are unaffected — they have a human to steer.

These tests exercise the guard logic directly (the conditional block in
``conversation_loop._run_conversation_impl``) without spinning up a real
AIAgent / provider, following the same pure-logic style as
``test_loop_guard.py``.
"""

from __future__ import annotations

from agent.conversation_loop import _SUBAGENT_REGROUND_INTERVAL


def _build_reground_message(
    api_call_count: int,
    platform: str,
    original_user_message: str,
    user_message: str = "",
    interval: int | None = None,
) -> str | None:
    """Replicate the re-grounding block from ``_run_conversation_impl``.

    Returns the injected user-message content, or ``None`` if the guard
    did not fire.  This mirrors the exact conditional logic in the
    conversation loop so we can unit-test it without a full agent.
    """
    rg_interval = interval if interval is not None else _SUBAGENT_REGROUND_INTERVAL
    if not (
        platform == "subagent"
        and rg_interval > 0
        and api_call_count > 0
        and api_call_count % rg_interval == 0
    ):
        return None

    rg_goal = original_user_message or user_message or ""
    if not rg_goal or not isinstance(rg_goal, str):
        return None
    rg_goal = rg_goal.strip()
    if len(rg_goal) > 500:
        rg_goal = rg_goal[:500] + "…"
    return (
        f"[task-reminder] You are a subagent working on a "
        f"delegated task. Your original goal (re-grounded "
        f"at iteration {api_call_count}): {rg_goal}\n\n"
        f"Re-check: are your current tool calls advancing "
        f"this goal? If not, change your approach."
    )


class TestRegroundFiring:
    """Verify the nudge fires ONLY at the right iteration / platform."""

    def test_fires_at_interval_for_subagent(self):
        msg = _build_reground_message(
            api_call_count=10,
            platform="subagent",
            original_user_message="Fix the bug in parser.py",
        )
        assert msg is not None
        assert "[task-reminder]" in msg
        assert "Fix the bug in parser.py" in msg
        assert "iteration 10" in msg

    def test_fires_at_multiple_of_interval(self):
        msg = _build_reground_message(
            api_call_count=20,
            platform="subagent",
            original_user_message="Run the test suite",
        )
        assert msg is not None
        assert "iteration 20" in msg
        assert "Run the test suite" in msg

    def test_does_not_fire_below_interval(self):
        for i in range(1, _SUBAGENT_REGROUND_INTERVAL):
            assert (
                _build_reground_message(
                    api_call_count=i,
                    platform="subagent",
                    original_user_message="do stuff",
                )
                is None
            )

    def test_does_not_fire_for_cli(self):
        assert (
            _build_reground_message(
                api_call_count=10,
                platform="cli",
                original_user_message="do stuff",
            )
            is None
        )

    def test_does_not_fire_for_gateway(self):
        assert (
            _build_reground_message(
                api_call_count=10,
                platform="telegram",
                original_user_message="do stuff",
            )
            is None
        )

    def test_does_not_fire_at_zero(self):
        assert (
            _build_reground_message(
                api_call_count=0,
                platform="subagent",
                original_user_message="do stuff",
            )
            is None
        )

    def test_disabled_when_interval_zero(self):
        assert (
            _build_reground_message(
                api_call_count=10,
                platform="subagent",
                original_user_message="do stuff",
                interval=0,
            )
            is None
        )


class TestRegroundContent:
    """Verify the injected message is well-formed."""

    def test_truncates_long_goals(self):
        long_goal = "x" * 600
        msg = _build_reground_message(
            api_call_count=10,
            platform="subagent",
            original_user_message=long_goal,
        )
        assert msg is not None
        # Truncated to 500 chars + ellipsis
        assert "x" * 500 in msg
        assert "…" in msg
        assert ("x" * 600) not in msg

    def test_falls_back_to_user_message(self):
        msg = _build_reground_message(
            api_call_count=10,
            platform="subagent",
            original_user_message="",
            user_message="fallback goal",
        )
        assert msg is not None
        assert "fallback goal" in msg

    def test_empty_goal_does_not_fire(self):
        assert (
            _build_reground_message(
                api_call_count=10,
                platform="subagent",
                original_user_message="",
                user_message="",
            )
            is None
        )

    def test_strips_whitespace(self):
        msg = _build_reground_message(
            api_call_count=10,
            platform="subagent",
            original_user_message="  spaced goal  ",
        )
        assert msg is not None
        assert "spaced goal" in msg
        # No leading/trailing spaces from the original
        assert "  spaced goal  " not in msg

    def test_custom_interval(self):
        """A non-default interval changes the firing cadence."""
        # At interval=5, iteration 5 should fire
        msg = _build_reground_message(
            api_call_count=5,
            platform="subagent",
            original_user_message="do stuff",
            interval=5,
        )
        assert msg is not None
        assert "iteration 5" in msg

        # At interval=5, iteration 10 should also fire
        msg = _build_reground_message(
            api_call_count=10,
            platform="subagent",
            original_user_message="do stuff",
            interval=5,
        )
        assert msg is not None

        # At interval=5, iteration 3 should NOT fire
        assert (
            _build_reground_message(
                api_call_count=3,
                platform="subagent",
                original_user_message="do stuff",
                interval=5,
            )
            is None
        )


class TestRegroundIntervalConfig:
    """Verify the default interval constant is sensible."""

    def test_default_interval_is_ten(self):
        assert _SUBAGENT_REGROUND_INTERVAL == 10

    def test_default_interval_positive(self):
        assert _SUBAGENT_REGROUND_INTERVAL > 0
