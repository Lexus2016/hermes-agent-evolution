"""Runtime test for #624: unattended cron turns enforce the loop-guard
instead of only nudging. Advisory nudges are routinely ignored by the model
with no human present to course-correct (observed: 9 warnings ignored, 65
consecutive terminal calls in one real session). Once the SAME stuck run has
been nudged ``CRON_LOOP_GUARD_HARD_STOP_THRESHOLD`` times on
``agent.platform == "cron"``, the turn must end as a failure instead of
nudging again. Interactive surfaces are unaffected — this is exercised in
``tests/agent/test_loop_guard.py::TestShouldCronHardStop`` at the pure-function
level; this file proves the actual conversation_loop wiring.
"""

import json
import uuid
from types import SimpleNamespace
from unittest.mock import patch

from run_agent import AIAgent


def _make_tool_defs(*names: str) -> list[dict]:
    return [
        {
            "type": "function",
            "function": {
                "name": name,
                "description": f"{name} tool",
                "parameters": {"type": "object", "properties": {}},
            },
        }
        for name in names
    ]


def _mock_tool_call(name="terminal", arguments="{}", call_id=None):
    return SimpleNamespace(
        id=call_id or f"call_{uuid.uuid4().hex[:8]}",
        type="function",
        function=SimpleNamespace(name=name, arguments=arguments),
    )


def _mock_response(content="Hello", finish_reason="stop", tool_calls=None):
    msg = SimpleNamespace(content=content, tool_calls=tool_calls)
    choice = SimpleNamespace(message=msg, finish_reason=finish_reason)
    return SimpleNamespace(choices=[choice], model="test/model", usage=None)


def _make_agent(*tool_names: str, max_iterations: int = 20, platform: str | None = None) -> AIAgent:
    with (
        patch("run_agent.get_tool_definitions", return_value=_make_tool_defs(*tool_names)),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("hermes_cli.config.load_config", return_value={}),
        patch("run_agent.OpenAI"),
    ):
        agent = AIAgent(
            api_key="test-key-1234567890",
            base_url="https://openrouter.ai/api/v1",
            max_iterations=max_iterations,
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
            platform=platform,
        )
    from unittest.mock import MagicMock

    agent.client = MagicMock()
    agent._cached_system_prompt = "You are helpful."
    agent._use_prompt_caching = False
    agent.tool_delay = 0
    agent.compression_enabled = False
    agent.save_trajectories = False
    return agent


def _repeated_terminal_run(n: int, command: str = "echo hi"):
    """n identical successful terminal tool-call responses, then a final
    text-only "stop" response as a safety net in case the hard stop doesn't
    fire and the loop keeps going.

    Note: each fired nudge is injected into ``messages`` as a ``role="user"``
    entry, which resets what ``current_run_signature`` sees as the trailing
    single-tool run (it stops walking backward at the first non-tool
    message). So after the first nudge (at raw call 4 for a mutating tool),
    the run must regrow by a full ``repeat_threshold`` worth of calls from
    that injection point — not from the original start — before the re-nudge
    growth check re-admits a second nudge. Empirically the cron hard stop
    fires at raw call 12 for ``terminal`` (mutating, repeat_threshold=4)."""
    responses = [
        _mock_response(
            content="",
            finish_reason="tool_calls",
            tool_calls=[_mock_tool_call("terminal", json.dumps({"command": command}), f"c{i}")],
        )
        for i in range(n)
    ]
    responses.append(_mock_response(content="done", finish_reason="stop", tool_calls=None))
    return responses


def test_cron_platform_hard_stops_after_repeated_advisory_nudges():
    agent = _make_agent("terminal", max_iterations=20, platform="cron")
    agent.client.chat.completions.create.side_effect = _repeated_terminal_run(15)

    with (
        patch("run_agent.handle_function_call", return_value="command completed"),
        patch.object(agent, "_persist_session"),
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
    ):
        result = agent.run_conversation("run the same command repeatedly")

    assert result["failed"] is True
    assert result["turn_exit_reason"] == "loop_guard_cron_hard_stop"
    assert "loop-guard" in result["final_response"].lower()
    # Stopped well short of exhausting the iteration budget or the full
    # 15-call canned run — proves it broke out of the loop early rather than
    # running to the safety-net "done" response. Empirically verified at 12.
    assert result["api_calls"] < agent.max_iterations
    assert result["api_calls"] == 12


def test_resolved_spiral_state_does_not_leak_into_a_later_unrelated_tool_run():
    """Regression: an earlier `terminal` spiral that racked up multiple
    advisory nudges and was then resolved (the agent moved on to healthy,
    varied work on a different tool) must NOT leave stale
    `_loop_guard_warning_count` / `_loop_guard_nudged` state behind. Without
    resetting the tracked-tool identity on every switch (not just when the
    run goes fully quiet), a LATER, unrelated spiral on the SAME tool name
    would inherit the old warning count and could cron-hard-stop far earlier
    or later than its own two genuine warnings warrant.

    Uses platform=None (interactive) so the spiral is free to accumulate
    several real nudges without being cut short by the cron hard-stop —
    that lets the stale count grow past the trivial "exactly
    repeat_threshold" case, which is otherwise indistinguishable from a
    fresh run's first nudge count and would mask the bug. The assertion is
    on internal state directly (not an emergent hard-stop position), since
    at the shipped CRON_LOOP_GUARD_HARD_STOP_THRESHOLD=2 any cron session
    that reached a 2nd nudge would already have stopped before it could
    "resolve peacefully" — this scenario can only be exercised end-to-end
    on a surface where nudges are purely advisory.
    """
    agent = _make_agent("terminal", "read_file", max_iterations=30, platform=None)

    first_spiral = [
        _mock_response(
            content="",
            finish_reason="tool_calls",
            tool_calls=[_mock_tool_call("terminal", json.dumps({"command": "echo hi"}), f"a{i}")],
        )
        for i in range(20)  # empirically: 2 nudges accumulate over 20 calls
        # (each fired nudge resets the visible run per _repeated_terminal_run's
        # docstring, so nudge #2 needs a full regrowth from the injection
        # point, not just +3 from the original start)
    ]
    healthy_interlude = [
        _mock_response(
            content="",
            finish_reason="tool_calls",
            tool_calls=[_mock_tool_call("read_file", json.dumps({"path": f"/tmp/f{i}.txt"}), f"b{i}")],
        )
        for i in range(3)  # varied args, different tool -> lets the reset settle
    ]
    trailing_done = [_mock_response(content="done", finish_reason="stop", tool_calls=None)]
    agent.client.chat.completions.create.side_effect = (
        first_spiral + healthy_interlude + trailing_done
    )

    with (
        patch("run_agent.handle_function_call", return_value="command completed"),
        patch.object(agent, "_persist_session"),
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
    ):
        result = agent.run_conversation("do varied work, some of it repetitive")

    # The first spiral genuinely racked up multiple warnings (2, verified via
    # the loop-guard nudge messages actually delivered to the model)...
    nudge_msgs = [
        m for m in result["messages"]
        if m.get("role") == "user" and "[loop-guard]" in str(m.get("content", ""))
    ]
    assert len(nudge_msgs) == 2
    # ...but by the time healthy read_file work follows, the tracked tool and
    # warning count must reflect ONLY that new, unrelated activity.
    assert agent._loop_guard_tracked_tool == "read_file"
    assert agent._loop_guard_warning_count == 0


def test_non_cron_platform_keeps_nudging_advisory_only():
    """Same stuck-run shape on an interactive surface (platform=None) must
    NOT hard-stop — a human is present to course-correct there."""
    agent = _make_agent("terminal", max_iterations=20, platform=None)
    agent.client.chat.completions.create.side_effect = _repeated_terminal_run(9)

    with (
        patch("run_agent.handle_function_call", return_value="command completed"),
        patch.object(agent, "_persist_session"),
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
    ):
        result = agent.run_conversation("run the same command repeatedly")

    assert result.get("failed") is not True
    assert result["turn_exit_reason"] != "loop_guard_cron_hard_stop"
    assert result["final_response"] == "done"
