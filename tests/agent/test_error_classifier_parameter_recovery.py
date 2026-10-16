"""Focused regression coverage split from the runtime integration suite."""

from tests.agent.test_error_classifier import (
    FailoverReason,
    MockAPIError,
    _extract_status_code,
    classify_api_error,
    make_jwt,
    pytest,
)

class TestServerInjectedParameterRejection:
    """A 400 blaming a parameter the client never sent is a server-side flake.

    The Codex backend (chatgpt.com/backend-api/codex) intermittently adds
    ``prompt_cache_retention`` to its own upstream call and then rejects it,
    so an identical request succeeds on retry ~80% of the time.  Hermes never
    sends that field on this route, so the 400 is not a deterministic
    request-shape error and must stay retryable instead of aborting the turn.
    """

    RETENTION_BODY = {
        "message": "prompt_cache_retention is not supported on this model",
        "type": "invalid_request_error",
        "param": "prompt_cache_retention",
        "code": "invalid_parameter",
    }

    def test_codex_retention_400_is_retryable_server_error(self):
        e = MockAPIError(
            "Error code: 400 - {'error': {'message': 'prompt_cache_retention "
            "is not supported on this model', 'type': 'invalid_request_error', "
            "'param': 'prompt_cache_retention', 'code': 'invalid_parameter'}}",
            status_code=400,
            body=dict(self.RETENTION_BODY),
        )
        result = classify_api_error(
            e,
            provider="openai-codex",
            model="gpt-5.6-sol",
            approx_tokens=546912,
            context_length=272000,
            num_messages=576,
        )
        assert result.reason == FailoverReason.server_error
        assert result.retryable is True
        # Retrying the identical request is the recovery — do NOT enter the
        # compression loop (the context was never the problem).
        assert result.should_compress is False

    def test_codex_retention_400_nested_error_body_is_retryable(self):
        """The same rejection arrives wrapped in an ``error`` envelope too."""
        e = MockAPIError(
            "prompt_cache_retention is not supported on this model",
            status_code=400,
            body={"error": dict(self.RETENTION_BODY)},
        )
        result = classify_api_error(
            e, provider="openai-codex", model="gpt-5.6-sol",
        )
        assert result.reason == FailoverReason.server_error
        assert result.retryable is True

    def test_codex_gateway_terse_retention_400_is_retryable(self):
        """The Codex gateway's own validator uses a bare ``detail`` body."""
        e = MockAPIError(
            "Unsupported parameter: prompt_cache_retention",
            status_code=400,
            body={"detail": "Unsupported parameter: prompt_cache_retention"},
        )
        result = classify_api_error(
            e, provider="openai-codex", model="gpt-5.6-sol",
        )
        assert result.reason == FailoverReason.server_error
        assert result.retryable is True

    def test_small_session_retention_400_is_still_retryable(self):
        """Must not depend on the context-size heuristic — a tiny request
        gets the identical spontaneous rejection (reproduced live)."""
        e = MockAPIError(
            "prompt_cache_retention is not supported on this model",
            status_code=400,
            body=dict(self.RETENTION_BODY),
        )
        result = classify_api_error(
            e,
            provider="openai-codex",
            model="gpt-5.6-sol",
            approx_tokens=50,
            num_messages=1,
        )
        assert result.reason == FailoverReason.server_error
        assert result.retryable is True

    def test_other_unsupported_parameter_400_stays_non_retryable(self):
        """Boundary: a genuine client-sent bad parameter is deterministic and
        must keep failing fast as a format_error (the existing behaviour)."""
        e = MockAPIError(
            "Unsupported parameter: 'max_tokens' is not supported with this "
            "model. Use 'max_completion_tokens' instead.",
            status_code=400,
            body={
                "message": "Unsupported parameter: 'max_tokens' is not supported.",
                "type": "invalid_request_error",
                "param": "max_tokens",
                "code": "unsupported_parameter",
            },
        )
        result = classify_api_error(
            e, provider="openai-codex", model="gpt-5.6-sol",
        )
        assert result.reason == FailoverReason.format_error
        assert result.retryable is False

    def test_retention_rejection_from_meta_host_stays_non_retryable(self):
        """Boundary: on api.meta.ai / Bedrock Mantle Hermes DOES send
        ``prompt_cache_retention`` deliberately, so a rejection there is a
        real client-side request error and must not be retried blindly."""
        e = MockAPIError(
            "prompt_cache_retention is not supported on this model",
            status_code=400,
            body=dict(self.RETENTION_BODY),
        )
        result = classify_api_error(
            e, provider="meta-ai", model="muse-spark-1.2",
        )
        assert result.reason == FailoverReason.format_error
        assert result.retryable is False

    @pytest.mark.parametrize("status_code", [500, 502])
    def test_retention_rejection_via_5xx_proxy_is_retryable(self, status_code):
        """Sibling path: a proxy in front of the route can surface the same
        injected-parameter rejection as 5xx, where the request-validation
        guard would also wrongly fail it fast as a format_error."""
        e = MockAPIError(
            "Unsupported parameter: prompt_cache_retention",
            status_code=status_code,
            body={"error": dict(self.RETENTION_BODY)},
        )
        result = classify_api_error(
            e, provider="openai-codex", model="gpt-5.6-sol",
        )
        assert result.reason == FailoverReason.server_error
        assert result.retryable is True

    @pytest.mark.parametrize("status_code", [500, 502])
    def test_other_bad_parameter_via_5xx_stays_non_retryable(self, status_code):
        """Boundary for the sibling path: the codex.nekos.me 502-on-bad-param
        behaviour must keep failing fast (regression guard for that fix)."""
        e = MockAPIError(
            "Unknown parameter: 'frequency_penalty'",
            status_code=status_code,
            body={"error": {"message": "Unknown parameter: 'frequency_penalty'",
                            "code": "unknown_parameter"}},
        )
        result = classify_api_error(e, provider="custom", model="m")
        assert result.reason == FailoverReason.format_error
        assert result.retryable is False

class TestNousWelcomeTier:
    """The Nous gateway's welcome-tier contract: a structured 429 body carries ``reason`` /
    ``retry_after`` / ``alternates`` / ``upgrade_url``; a 400/403 names the wrong host or a
    dark tier in its message. The parsed refusal rides ``error_context``."""

    @staticmethod
    def _refusal(reason, retry_after=0, **extra):
        body = {"status": 429, "message": "refused", "reason": reason, "retry_after": retry_after, **extra}
        return MockAPIError(f"Error code: 429 - {body}", status_code=429, body=body,
                            headers={"retry-after": str(retry_after)})

    def test_model_not_free_is_a_non_retryable_gate_with_fallback(self):
        err = self._refusal("model_not_free", alternates=["nous/welcome"], upgrade_url="https://portal.example/upgrade")
        result = classify_api_error(err, provider="nous", api_key=make_jwt(), model="gpt-5")
        assert result.reason == FailoverReason.model_not_found
        assert result.retryable is False
        assert result.should_fallback is True
        assert result.should_rotate_credential is False
        refusal = result.error_context["welcome_refusal"]
        assert refusal["reason"] == "model_not_free"
        assert refusal["alternates"] == ["nous/welcome"]
        assert refusal["upgrade_url"] == "https://portal.example/upgrade"

    def test_feature_not_free_is_the_same_gate(self):
        result = classify_api_error(self._refusal("feature_not_free"), provider="nous", api_key=make_jwt())
        assert result.reason == FailoverReason.model_not_found
        assert result.retryable is False

    @pytest.mark.parametrize("reason", ["at_capacity", "admission_closed", "rate_limited"])
    def test_capacity_refusals_are_rate_limits_that_honour_retry_after(self, reason):
        result = classify_api_error(self._refusal(reason, retry_after=30), provider="nous", api_key=make_jwt(), model="nous/welcome")
        assert result.reason == FailoverReason.rate_limit
        assert result.retryable is True
        assert result.should_fallback is True
        ctx = result.error_context
        assert ctx["welcome_refusal"]["retry_after"] == 30
        assert ctx["reset_at"] > 0

    def test_retry_after_zero_carries_no_reset(self):
        result = classify_api_error(self._refusal("at_capacity", retry_after=0), provider="nous", api_key=make_jwt())
        assert "reset_at" not in result.error_context

    def test_anonymous_jwt_on_the_paid_host_is_deterministic(self):
        body = {"status": 400, "message": "Anonymous accounts must use https://welcome-api.nousresearch.com for inference."}
        err = MockAPIError(f"Error code: 400 - {body}", status_code=400, body=body)
        result = classify_api_error(err, provider="nous", api_key=make_jwt(), model="nous/welcome")
        assert result.reason == FailoverReason.format_error
        assert result.retryable is False and result.should_fallback is True
        assert result.error_context["welcome_route"] == "anon_on_paid_host"

    def test_named_caller_on_the_welcome_host_is_deterministic(self):
        body = {"status": 400, "message": "This endpoint serves anonymous Hermes Agent accounts only. Use https://inference-api.nousresearch.com with your API key or signed-in account."}
        err = MockAPIError(f"Error code: 400 - {body}", status_code=400, body=body)
        result = classify_api_error(err, provider="nous", api_key=make_jwt(account_tier="free"))
        assert result.error_context["welcome_route"] == "named_on_welcome_host"
        assert result.retryable is False

    def test_dark_tier_403_never_triggers_a_credential_refresh(self):
        body = {"status": 403, "message": "Anonymous accounts are not accepted by this API right now."}
        err = MockAPIError(f"Error code: 403 - {body}", status_code=403, body=body)
        result = classify_api_error(err, provider="nous", api_key=make_jwt(), model="nous/welcome")
        assert result.reason == FailoverReason.auth_permanent
        assert result.retryable is False and result.should_fallback is True
        assert result.should_rotate_credential is False
        assert result.error_context["welcome_route"] == "tier_disabled"

    def test_ordinary_403_is_untouched(self):
        result = classify_api_error(MockAPIError("forbidden", status_code=403, body={"message": "forbidden"}), provider="nous", api_key=make_jwt())
        assert result.reason == FailoverReason.auth
        assert "welcome_route" not in result.error_context

class TestAuthErrorNamesOffRouteEndpoint:
    """#113719: an auth refusal from a route that is not the provider's own endpoint names the host."""

    _BODY = {"error": {"code": "api_key_not_supported", "message": "API keys are not supported by this endpoint."}}

    def test_stale_base_url_names_contacted_host(self):
        e = MockAPIError("Unauthorized", status_code=401, body=self._BODY)
        result = classify_api_error(e, provider="anthropic", model="claude", base_url="https://chatgpt.com/backend-api/codex")
        assert result.reason == FailoverReason.auth
        assert result.message == "API keys are not supported by this endpoint. (endpoint: chatgpt.com)"

    def test_stock_endpoint_and_no_base_url_keep_plain_message(self):
        e = MockAPIError("Unauthorized", status_code=401, body=self._BODY)
        for base_url in ("", "https://api.anthropic.com/v1"):
            result = classify_api_error(e, provider="anthropic", model="claude", base_url=base_url)
            assert result.message == "API keys are not supported by this endpoint.", base_url

class TestBodyCarriedStatus:
    """An in-stream SSE error object's numeric ``code`` classifies like the equivalent HTTP
    response (#121270)."""

    def test_top_level_code_and_status_keys_also_count(self):
        assert _extract_status_code(MockAPIError("x", body={"code": 503})) == 503
        assert _extract_status_code(MockAPIError("x", body={"error": {"http_status": 502}})) == 502

    def test_403_ban_is_auth_not_transient_retry(self):
        body = {"error": {"code": 403, "message": "Your account has been banned by the upstream provider",
                          "metadata": {"provider_name": "acme"}}}
        result = classify_api_error(MockAPIError("Error code: 403", body=body), provider="custom")
        assert result.status_code == 403
        assert result.reason == FailoverReason.auth
        assert result.retryable is False
        assert result.should_fallback is True

class TestStreamingRenderFormatError:
    """Status-less Jinja render failures (LM Studio / llama.cpp) fail over; see #62662."""

    def test_error_rendering_no_status_is_format_error(self):
        e = MockAPIError("Error rendering prompt with jinja template: ...")
        result = classify_api_error(e, provider="lm-studio", model="x")
        assert result.reason == FailoverReason.format_error
        assert result.retryable is False
        assert result.should_fallback is True

    def test_render_message_with_status_uses_http_path(self):
        e = MockAPIError("Error rendering prompt with jinja template: ...", status_code=500)
        result = classify_api_error(e, provider="lm-studio", model="x")
        assert result.reason != FailoverReason.format_error

class TestConnectionMessagePatterns:
    """Generic-typed connect/DNS failures route to the transport bucket."""

    @pytest.mark.parametrize("message", [
        "connect ECONNREFUSED 127.0.0.1:11434",
        "Connection refused by proxy",
        "getaddrinfo failed",
        "getaddrinfo ENOTFOUND api.example.com",
        "[Errno -3] Temporary failure in name resolution",
        "[Errno 8] nodename nor servname provided, or not known",
        "getaddrinfo EAI_AGAIN openrouter.ai",
        "Name or service not known",
        "No route to host",
        "[Errno 101] Network is unreachable",
        "fetch failed",
        "TypeError: Failed to fetch",
        "upstream connect error or disconnect/reset before headers",
    ])
    def test_generic_exception_with_connect_failure_message_is_timeout(self, message):
        # RuntimeError — NOT in _TRANSPORT_ERROR_TYPES, not a ConnectionError
        # subclass, no status code. Without message matching this falls to
        # FailoverReason.unknown and misses the eager transport fallback.
        result = classify_api_error(RuntimeError(message))
        assert result.reason == FailoverReason.timeout, message
        assert result.retryable is True
        assert result.should_compress is False

    def test_connect_failure_never_routes_to_compression_on_large_session(self):
        # A connection that was never established is not an overflow signal,
        # even when the session is huge (the disconnect+large-session
        # heuristic must not apply to connect-phase failures).
        result = classify_api_error(
            RuntimeError("connect ECONNREFUSED 10.0.0.5:443"),
            approx_tokens=180000, context_length=200000, num_messages=400,
        )
        assert result.reason == FailoverReason.timeout
        assert result.should_compress is False

    def test_midstream_disconnect_patterns_still_use_disconnect_path(self):
        # "connection reset by peer" is deliberately NOT in the connect-phase
        # list — it stays on the _SERVER_DISCONNECT_PATTERNS path, which
        # routes large sessions to context-overflow compression.
        result = classify_api_error(
            RuntimeError("Connection reset by peer"),
            approx_tokens=180000, context_length=200000, num_messages=400,
        )
        assert result.reason == FailoverReason.context_overflow
        assert result.should_compress is True

    def test_plain_unknown_error_still_unknown(self):
        # Guard against over-matching: an unrelated message stays unknown.
        result = classify_api_error(RuntimeError("something exploded"))
        assert result.reason == FailoverReason.unknown

class TestThrottleVsOverflowDisambiguation:
    """Throttle messages that mention tokens must NOT route to compression."""

    def test_bedrock_throttling_too_many_tokens_is_rate_limit(self):
        # AWS Bedrock (and some proxies) surface throttling as
        # "Throttling error: Too many tokens, please wait before trying
        # again." — the "too many tokens" fragment sits in
        # _CONTEXT_OVERFLOW_PATTERNS, so before the "throttling" rate-limit
        # pattern this compressed a healthy session on every throttle.
        e = Exception(
            "Throttling error: Too many tokens, please wait before trying again."
        )
        result = classify_api_error(e, provider="bedrock", model="claude")
        assert result.reason == FailoverReason.rate_limit
        assert result.should_compress is False

    def test_plain_too_many_tokens_still_overflow(self):
        # Without any throttle wording, "Too many tokens" remains a
        # context-overflow signal (Z.AI / GLM family wording).
        e = Exception("Too many tokens")
        result = classify_api_error(e, provider="zai", model="glm-5")
        assert result.reason == FailoverReason.context_overflow
        assert result.should_compress is True
