"""Tests for cross-provider fallback on 402 billing pool exhaustion.

Covers issue #1043: when all credentials in a provider's pool are exhausted
for a billing (402) error, ``recover_with_credential_pool`` should attempt
to switch to the next provider in the fallback chain instead of leaving the
session blocked.
"""

from unittest.mock import MagicMock, patch

from agent.error_classifier import FailoverReason


def _make_agent_with_pool(*, pool_entries=1, has_fallback_chain=True):
    """Build a minimal agent + exhausted pool for billing-fallback tests."""
    from run_agent import AIAgent

    with patch.object(AIAgent, "__init__", lambda self, **kw: None):
        agent = AIAgent()

    entries = []
    for i in range(pool_entries):
        e = MagicMock(name=f"entry_{i}")
        e.id = f"cred-{i}"
        entries.append(e)

    pool = MagicMock()
    pool.has_credentials.return_value = True
    pool.provider = ""  # unscoped — avoid the provider-mismatch guard

    # Every rotation returns None → pool is immediately exhausted.
    pool.mark_exhausted_and_rotate = MagicMock(return_value=None)
    agent._credential_pool = pool
    agent._swap_credential = MagicMock()
    agent.log_prefix = ""
    agent.provider = "openai"
    agent.model = "gpt-4o"

    if has_fallback_chain:
        agent._fallback_chain = [{"provider": "anthropic", "model": "claude-3-5-sonnet"}]
    else:
        agent._fallback_chain = []
    agent._fallback_index = 0
    agent._fallback_activated = False
    return agent, pool


class TestBillingCrossProviderFallback:
    """402 with an exhausted pool should escalate to the fallback chain."""

    def test_billing_pool_exhausted_activates_fallback(self):
        agent, pool = _make_agent_with_pool(has_fallback_chain=True)
        agent._try_activate_fallback = MagicMock(return_value=True)

        recovered, has_retried = agent._recover_with_credential_pool(
            status_code=402, has_retried_429=False,
            classified_reason=FailoverReason.billing,
        )

        assert recovered is True
        assert has_retried is False
        pool.mark_exhausted_and_rotate.assert_called_once()
        agent._try_activate_fallback.assert_called_once_with(
            reason=FailoverReason.billing,
        )

    def test_billing_pool_exhausted_no_fallback_returns_false(self):
        """No fallback chain + exhausted pool → (False, ...) with a clear error."""
        agent, pool = _make_agent_with_pool(has_fallback_chain=False)
        agent._try_activate_fallback = MagicMock(return_value=False)

        recovered, has_retried = agent._recover_with_credential_pool(
            status_code=402, has_retried_429=False,
            classified_reason=FailoverReason.billing,
        )

        assert recovered is False
        assert has_retried is False
        agent._try_activate_fallback.assert_called_once_with(
            reason=FailoverReason.billing,
        )

    def test_billing_fallback_exception_does_not_mask_402(self):
        """If _try_activate_fallback raises, we still return False cleanly."""
        agent, pool = _make_agent_with_pool(has_fallback_chain=True)
        agent._try_activate_fallback = MagicMock(side_effect=RuntimeError("boom"))

        recovered, has_retried = agent._recover_with_credential_pool(
            status_code=402, has_retried_429=False,
            classified_reason=FailoverReason.billing,
        )

        assert recovered is False
        assert has_retried is False

    def test_billing_pool_has_credentials_still_rotates(self):
        """Sanity: a non-exhausted pool still rotates in-pool, no fallback call."""
        agent, pool = _make_agent_with_pool(pool_entries=2)
        next_entry = MagicMock(id="cred-next")
        pool.mark_exhausted_and_rotate = MagicMock(return_value=next_entry)
        agent._try_activate_fallback = MagicMock()

        recovered, has_retried = agent._recover_with_credential_pool(
            status_code=402, has_retried_429=False,
            classified_reason=FailoverReason.billing,
        )

        assert recovered is True
        assert has_retried is False
        agent._swap_credential.assert_called_once_with(next_entry)
        agent._try_activate_fallback.assert_not_called()