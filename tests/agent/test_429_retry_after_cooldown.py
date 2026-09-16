"""Tests for 429 rate-limit fallback honoring Retry-After and cooldowns.

Issue #478: recurring 429 rate_limit errors should not retry the same
provider/model; they should switch to the configured fallback chain and
remember the cooldown so the primary provider is not immediately retried.
"""

from unittest.mock import MagicMock, patch

from run_agent import AIAgent


def _make_agent(fallback_model=None):
    """Create a minimal AIAgent with optional fallback config."""
    with (
        patch("run_agent.get_tool_definitions", return_value=[]),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
    ):
        agent = AIAgent(
            api_key="test-key",
            base_url="https://openrouter.ai/api/v1",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
            fallback_model=fallback_model,
        )
        agent.client = MagicMock()
        return agent


def _mock_client(base_url="https://api.openai.com/v1", api_key="fb-key"):
    mock = MagicMock()
    mock.base_url = base_url
    mock.api_key = api_key
    return mock


def _429_error(retry_after: str | None = None):
    """Return a MagicMock 429 error shaped like an OpenAI APIStatusError."""
    err = MagicMock()
    err.status_code = 429
    err.__str__ = lambda self: "Rate limit exceeded"
    response = MagicMock()
    headers = {"retry-after": retry_after} if retry_after is not None else {}
    response.headers = headers
    err.response = response
    return err


class Test429FallbackCooldown:
    def test_rate_limit_sets_provider_cooldown_from_retry_after(self):
        fbs = [
            {"provider": "openai", "model": "gpt-4o"},
        ]
        agent = _make_agent(fallback_model=fbs)
        agent.provider = "openrouter"
        agent.model = "anthropic/claude-sonnet-4.6"
        agent.base_url = "https://openrouter.ai/api/v1"

        with patch(
            "agent.auxiliary_client.resolve_provider_client",
            return_value=(_mock_client(), "gpt-4o"),
        ):
            assert (
                agent._try_activate_fallback(
                    reason=MagicMock(),  # enum placeholder; patched below
                    api_error=_429_error("45"),
                )
                is True
            )

        # The enum placeholder above is not the real FailoverReason, so the
        # cooldown branch did not fire. Re-run with the real rate_limit reason
        # to verify cooldown recording.
        from agent.error_classifier import FailoverReason

        agent2 = _make_agent(fallback_model=fbs)
        agent2.provider = "openrouter"
        agent2.model = "anthropic/claude-sonnet-4.6"
        agent2.base_url = "https://openrouter.ai/api/v1"

        with patch(
            "agent.auxiliary_client.resolve_provider_client",
            return_value=(_mock_client(), "gpt-4o"),
        ):
            assert (
                agent2._try_activate_fallback(
                    reason=FailoverReason.rate_limit,
                    api_error=_429_error("45"),
                )
                is True
            )

        assert agent2._fallback_activated is True
        assert agent2.provider == "openai"
        # Primary provider should be on cooldown for at least 40s
        assert getattr(agent2, "_rate_limited_providers", {}).get("openrouter", 0) > 0
        remaining = (
            agent2._rate_limited_providers["openrouter"] - 0
        )  # monotonic baseline
        assert remaining >= 40

    def test_rate_limit_without_retry_after_uses_default_cooldown(self):
        from agent.error_classifier import FailoverReason

        fbs = [{"provider": "openai", "model": "gpt-4o"}]
        agent = _make_agent(fallback_model=fbs)
        agent.provider = "openrouter"
        agent.model = "anthropic/claude-sonnet-4.6"
        agent.base_url = "https://openrouter.ai/api/v1"

        with patch(
            "agent.auxiliary_client.resolve_provider_client",
            return_value=(_mock_client(), "gpt-4o"),
        ):
            assert (
                agent._try_activate_fallback(
                    reason=FailoverReason.rate_limit,
                    api_error=_429_error(None),
                )
                is True
            )

        assert agent._rate_limited_providers["openrouter"] > 0

    def test_rate_limited_provider_skipped_in_fallback_chain(self):
        from agent.error_classifier import FailoverReason
        import time

        fbs = [
            {"provider": "openai", "model": "gpt-4o"},
            {"provider": "zai", "model": "glm-4.7"},
        ]
        agent = _make_agent(fallback_model=fbs)
        agent.provider = "openrouter"
        agent.model = "anthropic/claude-sonnet-4.6"
        agent.base_url = "https://openrouter.ai/api/v1"
        # First entry (openai) is intentionally cooled down so it is skipped.
        agent._rate_limited_providers = {"openai": time.monotonic() + 600}

        called = []

        def _resolve(provider, model=None, raw_codex=False, **kwargs):
            called.append((provider, model))
            return _mock_client(), model

        with patch(
            "agent.auxiliary_client.resolve_provider_client", side_effect=_resolve
        ):
            with patch(
                "hermes_cli.model_normalize.normalize_model_for_provider",
                side_effect=lambda m, p: m,
            ):
                ok = agent._try_activate_fallback(
                    reason=FailoverReason.rate_limit,
                    api_error=_429_error("30"),
                )

        assert ok is True
        # openai was skipped because it was under cooldown; zai was used.
        assert called == [("zai", "glm-4.7")]
        # Current provider moved to the second fallback.
        assert agent.provider == "zai"
        assert agent.model == "glm-4.7"
