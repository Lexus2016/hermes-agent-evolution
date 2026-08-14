"""Tests for cron-context extended retry ceiling (issue #2376).

Slice A of #2374: raise the 429 retry ceiling for cron/unattended contexts
and widen jitter so brief provider spikes don't kill the pipeline stage.
"""
from unittest.mock import patch

from run_agent import AIAgent


def _make_agent(api_max_retries=None, cron_api_max_retries=None, platform="cli"):
    """Build an AIAgent with a mocked config tree."""
    cfg = {"agent": {}}
    if api_max_retries is not None:
        cfg["agent"]["api_max_retries"] = api_max_retries
    if cron_api_max_retries is not None:
        cfg["agent"]["cron_api_max_retries"] = cron_api_max_retries

    with patch("run_agent.OpenAI"), \
         patch("hermes_cli.config.load_config", return_value=cfg), \
         patch("hermes_cli.config.load_config_readonly", return_value=cfg):
        return AIAgent(
            api_key="test-key",
            base_url="https://openrouter.ai/api/v1",
            model="test/model",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
            platform=platform,
        )


def test_default_cron_api_max_retries_is_fifteen():
    """No config override → cron ceiling defaults to 15."""
    agent = _make_agent()
    assert agent._cron_api_max_retries == 15


def test_cron_api_max_retries_honors_config_override():
    """Setting agent.cron_api_max_retries in config propagates."""
    agent = _make_agent(cron_api_max_retries=20)
    assert agent._cron_api_max_retries == 20

    agent2 = _make_agent(cron_api_max_retries=1)
    assert agent2._cron_api_max_retries == 1


def test_cron_ceiling_does_not_lower_interactive_ceiling():
    """A cron ceiling below the interactive one must not reduce max_retries."""
    agent = _make_agent(api_max_retries=5, cron_api_max_retries=2)
    # The cron ceiling is only *raised* above the interactive default, never
    # lowered — interactive sessions keep their own api_max_retries.
    assert agent._cron_api_max_retries == 2
    assert agent._api_max_retries == 5
