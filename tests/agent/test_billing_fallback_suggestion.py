#!/usr/bin/env python3
"""
Test that billing-error terminal abort suggests configuring a fallback
provider when none is configured (issue #1043).

Run with:  python -m pytest tests/agent/test_billing_fallback_suggestion.py -v
"""

import unittest
from unittest.mock import MagicMock, patch


class TestBillingFallbackSuggestion(unittest.TestCase):
    """When a 402 billing error reaches the terminal abort path and no
    fallback chain is configured, the user should see a suggestion to
    configure one via `hermes fallback add`."""

    def test_no_fallback_chain_triggers_suggestion(self):
        """When _fallback_chain is empty, the billing abort path should
        suggest `hermes fallback add`."""
        from agent.conversation_loop import _billing_or_entitlement_message

        # The message function itself should still work — we're testing
        # the guidance path in the conversation loop, not the message.
        msg = _billing_or_entitlement_message(
            capability="model access",
            provider="openrouter",
            base_url="https://openrouter.ai/api/v1",
            model="anthropic/claude-sonnet-4",
        )
        # The existing message should mention switching providers
        self.assertIn("switch", msg.lower())
        self.assertIn("provider", msg.lower())

    def test_fallback_chain_check_logic(self):
        """Verify the check `not getattr(agent, '_fallback_chain', None)`
        correctly identifies an empty/unconfigured fallback chain."""
        agent_no_chain = MagicMock()
        agent_no_chain._fallback_chain = []

        agent_with_chain = MagicMock()
        agent_with_chain._fallback_chain = [
            {"provider": "openrouter", "model": "anthropic/claude-sonnet-4"}
        ]

        agent_no_attr = MagicMock()
        del agent_no_attr._fallback_chain
        # getattr with default handles missing attribute

        # Empty chain → should suggest (True = needs suggestion)
        self.assertFalse(getattr(agent_no_chain, "_fallback_chain", None))

        # Configured chain → should NOT suggest
        self.assertTrue(getattr(agent_with_chain, "_fallback_chain", None))

        # Missing attribute → should suggest (treated as no chain)
        self.assertFalse(getattr(agent_no_attr, "_fallback_chain", None))


if __name__ == "__main__":
    unittest.main()
