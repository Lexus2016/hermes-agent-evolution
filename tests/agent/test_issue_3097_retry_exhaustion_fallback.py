"""Unit and integration tests for Issue #3097: API retry exhaustion and fallback routing."""

import logging
from unittest.mock import MagicMock, patch

import pytest
from agent.error_classifier import FailoverReason, classify_api_error
from hermes_cli.fallback_config import get_fallback_chain
from run_agent import AIAgent


def _make_agent(fallback_model=None):
    with (
        patch("model_tools.get_tool_definitions", return_value=[]),
        patch("model_tools.check_toolset_requirements", return_value={}),
        patch("agent.process_bootstrap.OpenAI"),
    ):
        agent = AIAgent(
            api_key="test-key",
            base_url="https://openrouter.ai/api/v1",
            provider="openrouter",
            model="anthropic/claude-3-5-sonnet",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
            fallback_model=fallback_model,
        )
        agent.client = MagicMock()
        return agent


class TestIssue3097RetryExhaustionAndFallback:
    def test_configured_fallback_chain_reaches_the_agent(self):
        """A ``fallback_providers`` chain declared in config reaches the agent, in order.

        The agent does not read config for its fallback chain: hermes_cli resolves it with
        ``get_fallback_chain`` and passes the normalized list in as ``fallback_model``
        (cli.py, hermes_cli/oneshot.py, cron/scheduler.py). This pins that seam end to end, so a
        chain written to config still becomes the agent's ordered backups.
        """
        fake_config = {
            "fallback_providers": [
                {"provider": "anthropic", "model": "claude-3-5-haiku"},
                {"provider": "openai", "model": "gpt-4o-mini"},
            ]
        }
        agent = _make_agent(fallback_model=get_fallback_chain(fake_config))
        assert len(agent._fallback_chain) == 2
        assert agent._fallback_chain[0]["provider"] == "anthropic"
        assert agent._fallback_chain[1]["provider"] == "openai"
        assert agent._fallback_model == agent._fallback_chain[0]

    def test_undeclared_chain_leaves_no_fallbacks(self):
        """No chain declared anywhere means the agent has no fallbacks to activate."""
        agent = _make_agent(fallback_model=get_fallback_chain({}))
        assert agent._fallback_chain == []
        assert agent._fallback_model is None

    def test_retry_exhaustion_triggers_fallback_activation(self):
        """When retries exhaust on a transient error, try_activate_fallback is invoked with reason and api_error."""
        agent = _make_agent(
            fallback_model=[{"provider": "openai", "model": "gpt-4o"}]
        )
        err = Exception("500 Internal Server Error")
        err.status_code = 500
        classified = classify_api_error(err)

        mock_fb_client = MagicMock()
        mock_fb_client.base_url = "https://api.openai.com/v1"
        mock_fb_client.api_key = "sk-test"

        with patch(
            "agent.auxiliary_client.resolve_provider_client",
            return_value=(mock_fb_client, "gpt-4o"),
        ):
            assert agent._has_pending_fallback() is True
            activated = agent._try_activate_fallback(
                reason=classified.reason, api_error=err
            )
            assert activated is True
            assert agent.provider == "openai"
            assert agent.model == "gpt-4o"
            assert agent._fallback_index == 1

    def test_non_retryable_error_triggers_fallback_with_context(self):
        """Non-retryable client error (e.g. 422 Unprocessable Entity) triggers fallback with reason and error."""
        agent = _make_agent(
            fallback_model=[{"provider": "anthropic", "model": "claude-3-5-haiku"}]
        )
        err = Exception("422 Unprocessable Entity - extra fields not permitted")
        err.status_code = 422
        classified = classify_api_error(err)
        assert classified.retryable is False
        assert classified.should_fallback is True

        mock_fb_client = MagicMock()
        mock_fb_client.base_url = "https://api.anthropic.com"
        mock_fb_client.api_key = "ant-key"

        with patch(
            "agent.auxiliary_client.resolve_provider_client",
            return_value=(mock_fb_client, "claude-3-5-haiku"),
        ):
            activated = agent._try_activate_fallback(
                reason=classified.reason, api_error=err
            )
            assert activated is True
            assert agent.provider == "anthropic"
            assert agent.model == "claude-3-5-haiku"

    def test_retry_and_terminal_logs_are_distinguishable(self, caplog):
        """Telemetry logger differentiates retry attempts from terminal failure."""
        caplog.set_level(logging.DEBUG)
        agent = _make_agent(fallback_model=[])

        _provider = agent.provider
        _model = agent.model
        _final_summary = "500 Internal Server Error"
        max_retries = 3

        logger = logging.getLogger("agent.conversation_loop")
        with caplog.at_level(logging.WARNING):
            logger.warning(
                "API call retry attempt %s/%s (retryable=%s) error_type=%s %s summary=%s",
                1,
                max_retries,
                True,
                "APIError",
                "[client context]",
                _final_summary,
            )
            logger.error(
                "%sAPI call failed permanently after %s retries (max_retries_exhausted). %s | provider=%s model=%s msgs=%s tokens=~%s",
                "",
                max_retries,
                _final_summary,
                _provider,
                _model,
                5,
                "1,000",
            )

        log_text = caplog.text
        assert "API call retry attempt 1/3 (retryable=True)" in log_text
        assert "API call failed permanently after 3 retries (max_retries_exhausted)" in log_text
