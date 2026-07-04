"""Runtime tests for #704: two consecutive rate-limit errors with no recovery
path (no credential-pool rotation recovered, no fallback provider activated)
must end the turn with a structured diagnostic instead of burning the rest of
the retry budget against a provider whose quota window has not reset.

The classifier already marks 429 as ``FailoverReason.rate_limit`` with
rotation+fallback flags; the observed gap (12 recurrences of ``429:rate_limit``
in 7 days of real sessions) is the retry loop hammering the SAME exhausted
provider when neither recovery is available — the common cron shape: one
provider, no pool, no fallback chain. These tests drive ``run_conversation``
through mocked API errors and prove the fail-fast wiring end-to-end.
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


def _mock_response(content="Hello", finish_reason="stop", tool_calls=None):
    msg = SimpleNamespace(content=content, tool_calls=tool_calls)
    choice = SimpleNamespace(message=msg, finish_reason=finish_reason)
    return SimpleNamespace(choices=[choice], model="test/model", usage=None)


class _RateLimitError(Exception):
    """Bare 429 with no Retry-After header and no response body — the
    minimal shape ``classify_api_error`` maps to ``rate_limit``."""

    def __init__(self):
        super().__init__("Rate limit exceeded, please try again later")
        self.status_code = 429


def _make_agent(max_iterations: int = 10) -> AIAgent:
    with (
        patch("run_agent.get_tool_definitions", return_value=_make_tool_defs("terminal")),
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
        )
    from unittest.mock import MagicMock

    agent.client = MagicMock()
    agent._cached_system_prompt = "You are helpful."
    agent._use_prompt_caching = False
    agent.tool_delay = 0
    agent.compression_enabled = False
    agent.save_trajectories = False
    return agent


def _fast_backoff_patches():
    """Make the single allowed backoff instant so the tests don't sleep."""
    return (
        patch("agent.conversation_loop.jittered_backoff", return_value=0.01),
        patch(
            "agent.conversation_loop.adaptive_rate_limit_backoff",
            side_effect=lambda retry_count, **kw: (0.01, None),
        ),
    )


def test_two_consecutive_429s_without_recovery_fail_fast():
    agent = _make_agent()
    # Safety-net success responses after the two 429s: if the fail-fast does
    # NOT fire, the loop would consume one of these and the assertions on
    # failed/call_count below flag it loudly (instead of a StopIteration).
    agent.client.chat.completions.create.side_effect = [
        _RateLimitError(),
        _RateLimitError(),
        _mock_response(content="done"),
        _mock_response(content="done"),
    ]

    jb, arb = _fast_backoff_patches()
    with (
        jb,
        arb,
        patch.object(agent, "_persist_session"),
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
    ):
        result = agent.run_conversation("hello")

    assert result["failed"] is True
    assert result["failure_reason"] == "rate_limit"
    assert "rate limited twice in a row" in result["final_response"].lower()
    # Exactly one same-provider retry was allowed — the second consecutive
    # 429 ended the turn instead of continuing toward max_retries.
    assert agent.client.chat.completions.create.call_count == 2


def test_single_429_then_success_completes_normally():
    agent = _make_agent()
    agent.client.chat.completions.create.side_effect = [
        _RateLimitError(),
        _mock_response(content="done"),
    ]

    jb, arb = _fast_backoff_patches()
    with (
        jb,
        arb,
        patch.object(agent, "_persist_session"),
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
    ):
        result = agent.run_conversation("hello")

    assert result.get("failed") is not True
    assert result["final_response"] == "done"
    assert agent.client.chat.completions.create.call_count == 2


def test_fresh_api_call_gets_a_fresh_counter():
    """A 429 pair split across two DIFFERENT API calls (each preceded by a
    success) must not fail fast: ``TurnRetryState`` is created per API call,
    so non-consecutive rate limits never accumulate."""
    agent = _make_agent()
    tool_call = SimpleNamespace(
        id=f"call_{uuid.uuid4().hex[:8]}",
        type="function",
        function=SimpleNamespace(name="terminal", arguments=json.dumps({"command": "echo hi"})),
    )
    agent.client.chat.completions.create.side_effect = [
        _RateLimitError(),
        _mock_response(content="", finish_reason="tool_calls", tool_calls=[tool_call]),
        _RateLimitError(),
        _mock_response(content="done"),
    ]

    jb, arb = _fast_backoff_patches()
    with (
        jb,
        arb,
        patch("run_agent.handle_function_call", return_value="command completed"),
        patch.object(agent, "_persist_session"),
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
    ):
        result = agent.run_conversation("run a command")

    assert result.get("failed") is not True
    assert result["final_response"] == "done"
    assert agent.client.chat.completions.create.call_count == 4
