"""Tests for verify-before-retry wrapper (issue #1924)."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "scripts"))
from evolution_verify_before_retry import (  # noqa: E402
    VERIFY_FUNCTIONS,
    classify_failure,
    generate_idempotency_key,
    get_verify_fn,
    is_side_effecting,
    should_retry,
)


def test_classify_timeout():
    assert classify_failure("Connection timed out") == "timeout-after-dispatch"
    assert classify_failure("read timeout from server") == "timeout-after-dispatch"


def test_classify_conflict():
    assert (
        classify_failure("412 Precondition Failed: ETag mismatch")
        == "stale-version-conflict"
    )


def test_classify_partial():
    assert classify_failure("partial update applied") == "partial-state-update"


def test_classify_eventual():
    assert (
        classify_failure("not yet propagated, eventually consistent")
        == "eventual-consistency"
    )


def test_classify_safe_retry():
    assert classify_failure("404 not found") == "safe-retry"
    assert classify_failure("authentication failed") == "safe-retry"


def test_classify_unknown():
    assert classify_failure("something weird happened") == "unknown"


def test_is_side_effecting():
    assert is_side_effecting("agentmail__send_message") is True
    assert is_side_effecting("github__create_issue") is True
    assert is_side_effecting("x_twitter__create_tweet") is True
    assert is_side_effecting("read_file") is False
    assert is_side_effecting("web_search") is False


def test_idempotency_key_deterministic():
    args = {"to": "a@b.com", "subject": "test"}
    k1 = generate_idempotency_key("agentmail__send_message", args)
    k2 = generate_idempotency_key("agentmail__send_message", args)
    assert k1 == k2
    assert len(k1) == 32


def test_idempotency_key_differs():
    k1 = generate_idempotency_key("tool", {"a": 1})
    k2 = generate_idempotency_key("tool", {"a": 2})
    assert k1 != k2


def test_should_retry_non_side_effecting():
    assert should_retry("read_file", {}, "timeout") is True


def test_should_retry_safe_retry():
    assert should_retry("agentmail__send_message", {}, "404 not found") is True


def test_should_retry_timeout_no_verify():
    # No verify_fn: cautious, don't retry (avoid duplicates)
    assert should_retry("agentmail__send_message", {}, "timed out") is False


def test_should_retry_timeout_effect_occurred():
    # verify_fn returns True: effect happened, don't retry
    assert (
        should_retry(
            "agentmail__send_message", {}, "timed out", verify_fn=lambda a: True
        )
        is False
    )


def test_should_retry_timeout_effect_not_occurred():
    # verify_fn returns False: effect didn't happen, safe to retry
    assert (
        should_retry(
            "agentmail__send_message", {}, "timed out", verify_fn=lambda a: False
        )
        is True
    )


def test_should_retry_verify_exception():
    # verify_fn raises: cautious, don't retry
    def boom(a):
        raise RuntimeError("verify failed")

    assert (
        should_retry("agentmail__send_message", {}, "timed out", verify_fn=boom)
        is False
    )


def test_should_retry_unknown_failure():
    assert should_retry("agentmail__send_message", {}, "weird error") is True


def test_get_verify_fn():
    assert get_verify_fn("agentmail__send_message") is not None
    assert get_verify_fn("github__create_issue") is not None
    assert get_verify_fn("x_twitter__create_tweet") is not None
    assert get_verify_fn("read_file") is None


def test_verify_functions_registry():
    assert len(VERIFY_FUNCTIONS) >= 3
    for name, fn in VERIFY_FUNCTIONS.items():
        assert callable(fn)
