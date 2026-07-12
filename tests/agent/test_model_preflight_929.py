"""Tests for #929: pre-flight provider/model validation.

#758 (PR #923) added the *reactive* recovery path — when a provider rejects a
model with ``model_not_found`` the turn ends with actionable guidance. #929
adds the *proactive* half: a structurally-unresolvable model config is caught
BEFORE the first provider request, so the run fails fast without burning a
round trip.

Two layers are covered:
  • unit — ``agent.model_preflight.check_model`` structural verdicts and the
    deliberate boundary (empty model and well-formed-unavailable are allowed).
  • behavioral — driving ``run_conversation`` proves an unresolvable model is
    caught pre-flight with NO provider call, a resolvable model proceeds
    untouched, and the empty-model default (a valid state) is unaffected.

The behavioral harness mirrors ``test_provider_error_recovery_758.py``.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from agent.conversation_loop import (
    FailoverReason,
    _user_actionable_provider_guidance,
)
from agent.model_preflight import PreflightMiss, check_model
from run_agent import AIAgent

_OPENROUTER = "https://openrouter.ai/api/v1"


# ── Unit: structural verdicts ────────────────────────────────────────────


def test_resolvable_model_passes_silently():
    # A well-formed slug: no colon-prefix, no whitespace → proceed (None).
    assert check_model("openrouter", "anthropic/claude-sonnet-4.6", _OPENROUTER) is None
    assert (
        check_model(
            "anthropic", "claude-sonnet-4-5-20250929", "https://api.anthropic.com"
        )
        is None
    )
    # Ollama-style model:tag is a real id — the suffix is non-empty.
    assert check_model("ollama", "qwen:0.5b", "http://localhost:11434/v1") is None


def test_empty_model_is_allowed_by_design():
    # Empty/blank == "use provider default" elsewhere; must NOT be failed closed.
    assert check_model("openrouter", "", _OPENROUTER) is None
    assert check_model("openrouter", "   ", _OPENROUTER) is None
    assert check_model("openrouter", None, _OPENROUTER) is None


def test_bare_provider_prefix_is_caught_with_suggestions():
    miss = check_model("openrouter", "openrouter:", _OPENROUTER)
    assert isinstance(miss, PreflightMiss)
    assert "bare" in miss.detail and "openrouter:" in miss.detail
    # Suggestions come from the LOCAL curated openrouter fallback_models.
    assert miss.suggestions, "expected curated fallback_models as suggestions"


def test_internal_whitespace_is_caught():
    miss = check_model("openrouter", "gpt 4o", _OPENROUTER)
    assert isinstance(miss, PreflightMiss)
    assert "whitespace" in miss.detail


def test_whitespace_skipped_for_local_endpoint():
    # Local/custom endpoints may expose ad-hoc ids — don't second-guess them.
    assert check_model("local", "my local model", "http://localhost:1234/v1") is None
    assert check_model("custom", "some model", "http://127.0.0.1:8080/v1") is None


def test_well_formed_unknown_model_is_not_rejected():
    # No cheap local signal proves a well-formed slug is unavailable → allow it
    # (that stays on #758's reactive path). Boundary guard.
    assert (
        check_model("openrouter", "vendor/model-that-was-dropped-v9", _OPENROUTER)
        is None
    )


# ── Unit: guidance reuse (not fork) ──────────────────────────────────────


def test_guidance_default_phase_is_byte_identical_to_758():
    # The reactive #758 message must be unchanged when ``phase`` is omitted.
    abort = _user_actionable_provider_guidance(
        FailoverReason.model_not_found, provider="openrouter", model="foo/bar-v9"
    )
    assert abort == (
        "💡 The model 'foo/bar-v9' was rejected by openrouter — not found or "
        "not available on this endpoint. Retrying will not help.\n"
        "What you can do:\n"
        "  • Check the model name for typos, or run `hermes model` to pick a valid one.\n"
        "  • Add a fallback model so this routes automatically: `hermes fallback add`."
    )


def test_preflight_phase_reuses_steps_with_a_preflight_lead():
    pre = _user_actionable_provider_guidance(
        FailoverReason.model_not_found,
        provider="openrouter",
        model="openrouter:",
        phase="preflight",
    )
    # Pre-flight-accurate lead ("no request was sent")...
    assert "Pre-flight check" in pre
    assert "no request was sent" in pre
    # ...but the SAME single-sourced recovery steps as #758.
    assert "hermes model" in pre
    assert "hermes fallback add" in pre


# ── Behavioral: drive run_conversation ───────────────────────────────────


def _mock_response(content="Hello", finish_reason="stop", tool_calls=None):
    msg = SimpleNamespace(content=content, tool_calls=tool_calls)
    choice = SimpleNamespace(message=msg, finish_reason=finish_reason)
    return SimpleNamespace(choices=[choice], model="test/model", usage=None)


def _make_agent(max_iterations: int = 10) -> AIAgent:
    with (
        patch("run_agent.get_tool_definitions", return_value=[]),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("hermes_cli.config.load_config", return_value={}),
        patch("run_agent.OpenAI"),
    ):
        agent = AIAgent(
            api_key="test-key-1234567890",
            base_url=_OPENROUTER,
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
    agent.provider = "openrouter"
    return agent


def _run(agent, prompt="hello"):
    with (
        patch.object(agent, "_persist_session"),
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
    ):
        return agent.run_conversation(prompt)


def test_unresolvable_model_caught_preflight_without_provider_call():
    agent = _make_agent()
    agent.model = "openrouter:"  # bare provider prefix — structurally invalid
    # Safety net: if pre-flight failed to short-circuit, this would be consumed.
    agent.client.chat.completions.create.side_effect = [
        _mock_response(content="should-not-be-reached")
    ]

    result = _run(agent)

    assert result["failed"] is True
    assert result.get("user_actionable") is True
    assert result["api_calls"] == 0
    fr = result["final_response"]
    assert "Pre-flight check" in fr
    assert "hermes model" in fr
    assert "hermes fallback add" in fr
    assert result["error"].startswith("model_preflight:")
    # The provider was NEVER contacted — the whole point of #929.
    assert agent.client.chat.completions.create.call_count == 0


def test_whitespace_model_caught_preflight_without_provider_call():
    agent = _make_agent()
    agent.model = "Claude Sonnet 4.5"  # pasted display name (spaces)

    result = _run(agent)

    assert result["failed"] is True
    assert result.get("user_actionable") is True
    assert result["api_calls"] == 0
    assert agent.client.chat.completions.create.call_count == 0


def test_resolvable_model_proceeds_to_provider():
    agent = _make_agent()
    agent.model = "anthropic/claude-sonnet-4.6"  # valid slug
    agent.client.chat.completions.create.side_effect = [_mock_response(content="ok")]

    result = _run(agent)

    # Pre-flight is silent for a valid model — the turn runs normally.
    assert result.get("failed") is not True
    assert result.get("user_actionable") is not True
    assert result["final_response"] == "ok"
    assert agent.client.chat.completions.create.call_count == 1


def test_empty_model_default_is_unaffected_regression_guard():
    # The empty-model "use provider default" state (as in the #758 harness)
    # must reach the provider exactly as before — pre-flight adds nothing.
    agent = _make_agent()
    agent.model = ""
    agent.client.chat.completions.create.side_effect = [_mock_response(content="ok")]

    result = _run(agent)

    assert result.get("failed") is not True
    assert result.get("user_actionable") is not True
    assert result["final_response"] == "ok"
    assert agent.client.chat.completions.create.call_count == 1
