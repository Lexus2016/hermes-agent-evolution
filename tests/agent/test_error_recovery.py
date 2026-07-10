"""Tests for agent.error_recovery (#826 increment 1)."""

from __future__ import annotations

from agent.error_recovery import (
    ClassifiedError,
    ErrorClass,
    RecoveryAction,
    classify_error,
    recommend_action,
    should_retry,
)


class TestClassifyError:
    def test_rate_limit_transient(self):
        for msg in ["rate limit exceeded", "HTTP 429 too many requests", "throttled"]:
            r = classify_error(msg)
            assert r.error_class == ErrorClass.TRANSIENT, msg

    def test_quota_transient(self):
        assert classify_error("quota exceeded").error_class == ErrorClass.TRANSIENT

    def test_auth_permanent(self):
        for msg in [
            "401 unauthorized",
            "403 forbidden",
            "API key invalid",
            "token expired",
        ]:
            r = classify_error(msg)
            assert r.error_class == ErrorClass.PERMANENT, msg

    def test_permission_denied_permanent(self):
        assert classify_error("permission denied").error_class == ErrorClass.PERMANENT

    def test_not_found_permanent(self):
        for msg in [
            "404 not found",
            "No such file or directory",
            "module does not exist",
        ]:
            r = classify_error(msg)
            assert r.error_class == ErrorClass.PERMANENT, msg

    def test_validation_permanent(self):
        for msg in [
            "400 bad request",
            "validation error",
            "invalid parameter",
            "JSON decode error",
        ]:
            r = classify_error(msg)
            assert r.error_class == ErrorClass.PERMANENT, msg

    def test_timeout_transient(self):
        for msg in [
            "Connection timed out",
            "deadline exceeded",
            "ECONNREFUSED",
            "connection reset",
        ]:
            r = classify_error(msg)
            assert r.error_class == ErrorClass.TRANSIENT, msg

    def test_5xx_transient(self):
        for msg in [
            "500 internal server error",
            "503 service unavailable",
            "502 bad gateway",
        ]:
            r = classify_error(msg)
            assert r.error_class == ErrorClass.TRANSIENT, msg

    def test_temporary_transient(self):
        assert (
            classify_error("temporary failure, retry again").error_class
            == ErrorClass.TRANSIENT
        )

    def test_critical_oom(self):
        assert classify_error("out of memory").error_class == ErrorClass.CRITICAL

    def test_critical_recursion(self):
        assert (
            classify_error("maximum recursion depth exceeded").error_class
            == ErrorClass.CRITICAL
        )

    def test_unknown_unclassifiable(self):
        r = classify_error("something weird happened")
        assert r.error_class == ErrorClass.UNKNOWN

    def test_status_code_429_transient(self):
        assert (
            classify_error("msg", status_code=429).error_class == ErrorClass.TRANSIENT
        )

    def test_status_code_5xx_transient(self):
        assert (
            classify_error("msg", status_code=503).error_class == ErrorClass.TRANSIENT
        )

    def test_status_code_4xx_permanent(self):
        assert (
            classify_error("msg", status_code=404).error_class == ErrorClass.PERMANENT
        )

    def test_status_code_takes_precedence(self):
        # Even if the message says "timeout", a 403 is permanent
        assert (
            classify_error("timeout", status_code=403).error_class
            == ErrorClass.PERMANENT
        )

    def test_tool_name_preserved(self):
        r = classify_error("timeout", tool_name="terminal")
        assert r.tool_name == "terminal"

    def test_matched_pattern_populated(self):
        r = classify_error("rate limit exceeded")
        assert r.matched_pattern  # non-empty

    def test_is_transient_is_permanent(self):
        assert ClassifiedError(ErrorClass.TRANSIENT).is_transient
        assert not ClassifiedError(ErrorClass.TRANSIENT).is_permanent
        assert ClassifiedError(ErrorClass.PERMANENT).is_permanent
        assert not ClassifiedError(ErrorClass.PERMANENT).is_transient

    def test_to_dict(self):
        r = classify_error("timeout", tool_name="read_file")
        d = r.to_dict()
        assert "error_class" in d
        assert d["tool_name"] == "read_file"


class TestRecommendAction:
    def test_transient_first_attempt_retry(self):
        assert recommend_action(ErrorClass.TRANSIENT, 1) == RecoveryAction.RETRY

    def test_transient_second_backoff(self):
        assert (
            recommend_action(ErrorClass.TRANSIENT, 2)
            == RecoveryAction.RETRY_WITH_BACKOFF
        )

    def test_transient_exhausted_fallback(self):
        assert recommend_action(ErrorClass.TRANSIENT, 5) == RecoveryAction.FALLBACK

    def test_permanent_fallback_immediately(self):
        assert recommend_action(ErrorClass.PERMANENT, 1) == RecoveryAction.FALLBACK

    def test_permanent_second_escalate(self):
        assert recommend_action(ErrorClass.PERMANENT, 2) == RecoveryAction.ESCALATE

    def test_critical_escalate(self):
        assert recommend_action(ErrorClass.CRITICAL, 1) == RecoveryAction.ESCALATE

    def test_unknown_retry_then_escalate(self):
        assert recommend_action(ErrorClass.UNKNOWN, 1) == RecoveryAction.RETRY
        assert recommend_action(ErrorClass.UNKNOWN, 2) == RecoveryAction.ESCALATE

    def test_custom_strategy(self):
        custom = {ErrorClass.TRANSIENT: [RecoveryAction.ABORT]}
        assert recommend_action(ErrorClass.TRANSIENT, 1, custom) == RecoveryAction.ABORT

    def test_empty_strategy_escalates(self):
        custom = {ErrorClass.TRANSIENT: []}
        assert (
            recommend_action(ErrorClass.TRANSIENT, 1, custom) == RecoveryAction.ESCALATE
        )


class TestShouldRetry:
    def test_transient_retryable(self):
        assert should_retry(ErrorClass.TRANSIENT, 1)
        assert should_retry(ErrorClass.TRANSIENT, 2)

    def test_permanent_not_retryable(self):
        assert not should_retry(ErrorClass.PERMANENT, 1)

    def test_max_attempts_limit(self):
        assert not should_retry(ErrorClass.TRANSIENT, 3, max_attempts=3)

    def test_unknown_retry_once(self):
        assert should_retry(ErrorClass.UNKNOWN, 1)
        assert not should_retry(ErrorClass.UNKNOWN, 2)
