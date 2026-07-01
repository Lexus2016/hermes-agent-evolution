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
    fire and the loop keeps going."""
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
    agent.client.chat.completions.create.side_effect = _repeated_terminal_run(9)

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
    # 9-call canned run — proves it broke out of the loop early rather than
    # running to the safety-net "done" response.
    assert result["api_calls"] < agent.max_iterations
    assert result["api_calls"] < 9


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
