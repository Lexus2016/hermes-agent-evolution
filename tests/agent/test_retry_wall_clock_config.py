"""Tests for per-turn wall-clock retry cap (issue #3113).

This is a bounded slice of the circuit-breaker work: it prevents
stream-drop / APIConnectionError retry loops from stalling a turn
indefinitely by capping total retry/backoff wall-clock time.
"""

from unittest.mock import patch

from run_agent import AIAgent


def _make_agent(
    api_retry_wall_clock_seconds=None,
    cron_api_retry_wall_clock_seconds=None,
    platform="cli",
):
    """Build an AIAgent with a mocked config tree."""
    cfg = {"agent": {}}
    if api_retry_wall_clock_seconds is not None:
        cfg["agent"]["api_retry_wall_clock_seconds"] = api_retry_wall_clock_seconds
    if cron_api_retry_wall_clock_seconds is not None:
        cfg["agent"]["cron_api_retry_wall_clock_seconds"] = (
            cron_api_retry_wall_clock_seconds
        )

    with (
        patch("run_agent.OpenAI"),
        patch("hermes_cli.config.load_config", return_value=cfg),
        patch("hermes_cli.config.load_config_readonly", return_value=cfg),
    ):
        return AIAgent(
            api_key="test-key",
            base_url="https://openrouter.ai/api/v1",
            model="test/model",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
            platform=platform,
        )


def test_default_wall_clock_caps():
    """No config override → defaults are 300s interactive / 600s cron."""
    agent = _make_agent()
    assert agent._api_retry_wall_clock_seconds == 300.0
    assert agent._cron_api_retry_wall_clock_seconds == 600.0


def test_wall_clock_caps_honor_config_override():
    """agent.api_retry_wall_clock_seconds and cron variant propagate."""
    agent = _make_agent(
        api_retry_wall_clock_seconds=90,
        cron_api_retry_wall_clock_seconds=120,
    )
    assert agent._api_retry_wall_clock_seconds == 90.0
    assert agent._cron_api_retry_wall_clock_seconds == 120.0


def test_wall_clock_caps_clamp_to_minimum():
    """Very small / invalid values are clamped to 30s to avoid zero/negative."""
    agent = _make_agent(api_retry_wall_clock_seconds=5)
    assert agent._api_retry_wall_clock_seconds == 30.0

    agent2 = _make_agent(api_retry_wall_clock_seconds="not-a-number")
    assert agent2._api_retry_wall_clock_seconds == 300.0


def test_cron_wall_clock_cap_is_distinct_from_interactive():
    """Only the cron cap is used in cron platform context."""
    agent = _make_agent(
        api_retry_wall_clock_seconds=90,
        cron_api_retry_wall_clock_seconds=300,
        platform="cron",
    )
    assert agent._cron_api_retry_wall_clock_seconds == 300.0
    assert agent._api_retry_wall_clock_seconds == 90.0
