"""Unit and integration tests for Issue #3097: API retry exhaustion and fallback routing."""

import logging
from unittest.mock import MagicMock, patch

import pytest
from agent.error_classifier import FailoverReason, classify_api_error
from run_agent import AIAgent


def _make_agent(fallback_model=None):
    with (
        patch("run_agent.get_tool_definitions", return_value=[]),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
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
    def test_agent_auto_loads_fallback_chain_from_config(self):
        """When fallback_model arg is None, agent loads configured fallback chain from config."""
        fake_config = {
            "fallback_providers": [
                {"provider": "anthropic", "model": "claude-3-5-haiku"},
                {"provider": "openai", "model": "gpt-4o-mini"},
            ]
        }
        with patch("hermes_cli.config.load_config_readonly", return_value=fake_config):
            agent = _make_agent(fallback_model=None)
            assert len(agent._fallback_chain) == 2
            assert agent._fallback_chain[0]["provider"] == "anthropic"
            assert agent._fallback_chain[1]["provider"] == "openai"

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
