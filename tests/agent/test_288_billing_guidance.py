"""Regression tests for issue #288.

HTTP 402 (billing / quota) provider errors must:
  * classify as FailoverReason.billing (non-retryable)
  * abort after credential-pool rotation and fallback have failed
  * surface a user-facing diagnostic that names 402 and points to the
    provider dashboard without echoing the raw error body.
"""

from __future__ import annotations


class MockAPIError(Exception):
    def __init__(
        self,
        message: str,
        status_code: int | None = None,
        body: dict | None = None,
    ):
        self.status_code = status_code
        self.body = body
        super().__init__(message)


class Test402BillingClassification:
    def test_plain_402_is_billing_non_retryable(self):
        from agent.error_classifier import FailoverReason, classify_api_error

        err = MockAPIError("Payment Required", status_code=402)
        result = classify_api_error(
            err, provider="openrouter", model="anthropic/claude-sonnet-4"
        )
        assert result.reason == FailoverReason.billing
        assert result.retryable is False
        assert result.should_fallback is True

    def test_402_with_usage_limit_transient_signal_is_rate_limit(self):
        from agent.error_classifier import FailoverReason, classify_api_error

        err = MockAPIError("usage limit exceeded, try again later", status_code=402)
        result = classify_api_error(err)
        assert result.reason == FailoverReason.rate_limit
        assert result.retryable is True


class TestBillingOrEntitlementMessage:
    def test_message_names_402_and_provider_dashboard(self):
        from agent.conversation_loop import _billing_or_entitlement_message

        msg = _billing_or_entitlement_message(
            capability="model access",
            provider="openrouter",
            base_url="https://openrouter.ai/api/v1",
            model="anthropic/claude-sonnet-4",
        )
        assert "HTTP 402" in msg
        assert "out of credit or quota" in msg
        assert "https://openrouter.ai/settings/credits" in msg

    def test_generic_provider_message_names_402(self):
        from agent.conversation_loop import _billing_or_entitlement_message

        msg = _billing_or_entitlement_message(
            capability="model access",
            provider="custom",
            base_url="https://example.com/v1",
            model="some-model",
        )
        assert "HTTP 402" in msg
        assert "out of credit or quota" in msg


class TestBillingAbortPredicate:
    """Mirror the is_client_error predicate used in conversation_loop.py."""

    def _mirror_is_client_error(
        self,
        *,
        classified_retryable: bool,
        classified_reason,
        classified_should_compress: bool = False,
        is_local_validation_error: bool = False,
    ) -> bool:
        from agent.error_classifier import FailoverReason

        return is_local_validation_error or (
            not classified_retryable
            and not classified_should_compress
            and classified_reason
            not in {
                FailoverReason.rate_limit,
                FailoverReason.overloaded,
                FailoverReason.context_overflow,
                FailoverReason.payload_too_large,
                FailoverReason.long_context_tier,
                FailoverReason.thinking_signature,
            }
        )

    def test_billing_triggers_client_error_abort(self):
        from agent.error_classifier import FailoverReason

        assert self._mirror_is_client_error(
            classified_retryable=False,
            classified_reason=FailoverReason.billing,
        )

    def test_rate_limit_does_not_abort(self):
        from agent.error_classifier import FailoverReason

        assert not self._mirror_is_client_error(
            classified_retryable=True,
            classified_reason=FailoverReason.rate_limit,
        )
