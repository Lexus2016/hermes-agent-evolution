"""Runtime tests for #758: provider-layer errors must route by class.

Two routes:
  • USER-ACTIONABLE (model_not_found, format_error/BadRequestError/
    ValidationException) — deterministic and user-fixable. The turn must end
    immediately (no generic retry spin) with a concise, actionable
    ``final_response`` telling the user what failed and what to do, not a bare
    provider summary.
  • RECOVERABLE (timeout, connection drop) — must stay on the existing
    retry/fallback path and NOT be diverted into the user-actionable abort.

These drive ``run_conversation`` through mocked API errors and assert on the
returned turn result, proving the classification is CONSUMED by the
recovery/messaging path (the gap #758 fixes) rather than merely logged.
The harness mirrors ``test_rate_limit_fail_fast.py``.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from agent.conversation_loop import (
    FailoverReason,
    _USER_ACTIONABLE_ABORT_REASONS,
    _user_actionable_provider_guidance,
)
from run_agent import AIAgent


# ── Test doubles ─────────────────────────────────────────────────────────


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


class _ModelNotFoundError(Exception):
    """404 that ``classify_api_error`` maps to ``model_not_found``."""

    def __init__(self):
        super().__init__(
            "The model `foo/bar-v9` does not exist or you do not have access to it"
        )
        self.status_code = 404


class _BadRequestError(Exception):
    """400 with an unknown-parameter signal → ``format_error``."""

    def __init__(self):
        super().__init__("Invalid request: unknown parameter 'reasoning_effort'")
        self.status_code = 400


class _TimeoutError(Exception):
    """No status code, message-classified as ``timeout`` (recoverable)."""

    def __init__(self):
        super().__init__("Request timed out after 60s")


def _make_agent(max_iterations: int = 10) -> AIAgent:
    with (
        patch(
            "run_agent.get_tool_definitions", return_value=_make_tool_defs("terminal")
        ),
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
    agent.client = MagicMock()
    agent._cached_system_prompt = "You are helpful."
    agent._use_prompt_caching = False
    agent.tool_delay = 0
    agent.compression_enabled = False
    agent.save_trajectories = False
    return agent


def _fast_backoff_patches():
    """Make backoff instant so the tests don't sleep on the retry path."""
    return (
        patch("agent.conversation_loop.jittered_backoff", return_value=0.01),
        patch(
            "agent.conversation_loop.adaptive_rate_limit_backoff",
            side_effect=lambda retry_count, **kw: (0.01, None),
        ),
    )


def _run(agent, prompt="hello"):
    jb, arb = _fast_backoff_patches()
    with (
        jb,
        arb,
        patch.object(agent, "_persist_session"),
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
    ):
        return agent.run_conversation(prompt)


# ── Unit: the guidance helper ────────────────────────────────────────────


def test_guidance_helper_covers_only_user_actionable_reasons():
    assert _USER_ACTIONABLE_ABORT_REASONS == frozenset({
        FailoverReason.model_not_found,
        FailoverReason.format_error,
    })

    model_hint = _user_actionable_provider_guidance(
        FailoverReason.model_not_found, provider="openrouter", model="foo/bar-v9"
    )
    assert model_hint is not None
    assert "foo/bar-v9" in model_hint
    assert "hermes model" in model_hint
    assert "hermes fallback add" in model_hint

    fmt_hint = _user_actionable_provider_guidance(
        FailoverReason.format_error, provider="local", model="test/model"
    )
    assert fmt_hint is not None
    assert "schema" in fmt_hint.lower()
    assert "hermes model" in fmt_hint

    # Reasons with their own dedicated branches (or recoverable ones) are not
    # handled here — helper returns None so the caller keeps existing behavior.
    assert (
        _user_actionable_provider_guidance(FailoverReason.auth, provider="x", model="y")
        is None
    )
    assert (
        _user_actionable_provider_guidance(
            FailoverReason.timeout, provider="x", model="y"
        )
        is None
    )


# ── Behavioral: USER-ACTIONABLE errors abort with guidance, no retry ─────


def test_model_not_found_surfaces_actionable_message_without_retry():
    agent = _make_agent()
    # Safety-net successes: if the loop wrongly retried instead of aborting,
    # call_count would exceed 1 and the assertions below flag it.
    agent.client.chat.completions.create.side_effect = [
        _ModelNotFoundError(),
        _mock_response(content="should-not-be-reached"),
    ]

    result = _run(agent)

    assert result["failed"] is True
    assert result.get("user_actionable") is True
    fr = result["final_response"]
    assert "hermes model" in fr
    assert "hermes fallback add" in fr
    # Deterministic error → aborted on the first classification, no retry spin.
    assert agent.client.chat.completions.create.call_count == 1


def test_bad_request_surfaces_actionable_message_without_retry():
    agent = _make_agent()
    agent.client.chat.completions.create.side_effect = [
        _BadRequestError(),
        _mock_response(content="should-not-be-reached"),
    ]

    result = _run(agent)

    assert result["failed"] is True
    assert result.get("user_actionable") is True
    fr = result["final_response"].lower()
    assert "schema" in fr
    assert "hermes model" in fr
    # Raw provider summary preserved for logs/telemetry.
    assert "unknown parameter" in result["error"].lower()
    assert agent.client.chat.completions.create.call_count == 1


# ── Behavioral: RECOVERABLE errors stay on the retry path ────────────────


def test_recoverable_timeout_retries_then_completes():
    """A timeout is recoverable — it must route to retry (not the
    user-actionable abort). One transient timeout then success completes the
    turn normally with no ``user_actionable`` flag."""
    agent = _make_agent()
    agent.client.chat.completions.create.side_effect = [
        _TimeoutError(),
        _mock_response(content="done"),
    ]

    result = _run(agent)

    assert result.get("failed") is not True
    assert result.get("user_actionable") is not True
    assert result["final_response"] == "done"
    # Retried the same call once, then succeeded — it did NOT abort on the
    # first error the way a user-actionable error does.
    assert agent.client.chat.completions.create.call_count == 2


def test_recoverable_timeout_is_not_diverted_to_user_actionable_abort():
    """A persistent timeout must exhaust the retry/fallback path — never the
    single-shot user-actionable abort. Contrast with model_not_found, which
    ends after exactly one call."""
    agent = _make_agent()
    agent.client.chat.completions.create.side_effect = _TimeoutError()

    result = _run(agent)

    # Recoverable errors are never tagged user_actionable...
    assert result.get("user_actionable") is not True
    # ...and they burn multiple retries rather than aborting on the first call.
    assert agent.client.chat.completions.create.call_count > 1
