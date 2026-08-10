"""Tests for persistent provider demotion (issue #2194).

After CONSECUTIVE_FAILURE_DEMOTION_THRESHOLD (3) exhaustion events with the
SAME non-transient error code (402/403/404), the cooldown is extended to 24h.
This prevents a permanently-dead provider from being retried every hour.
"""

import time
from unittest.mock import patch

from agent.credential_pool import (
    CONSECUTIVE_FAILURE_DEMOTION_THRESHOLD,
    EXHAUSTED_TTL_DEMOTED_SECONDS,
    EXHAUSTED_TTL_DEFAULT_SECONDS,
    PooledCredential,
    _bump_consecutive_failures,
    _exhausted_ttl,
    _get_consecutive_failures,
    _reset_consecutive_failures,
)


def _make_entry(error_code=None, cf=0):
    e = PooledCredential(
        provider="custom",
        id="test1",
        label="test",
        auth_type="api_key",
        priority=0,
        source="manual",
        access_token="tok",
        last_error_code=error_code,
    )
    if cf:
        e.extra["consecutive_failures"] = cf
    return e


class TestExhaustedTtlDemotion:
    def test_no_demotion_under_threshold(self):
        assert _exhausted_ttl(403, 0) == EXHAUSTED_TTL_DEFAULT_SECONDS
        assert _exhausted_ttl(403, 1) == EXHAUSTED_TTL_DEFAULT_SECONDS
        assert _exhausted_ttl(403, 2) == EXHAUSTED_TTL_DEFAULT_SECONDS

    def test_demotion_at_threshold(self):
        assert _exhausted_ttl(403, CONSECUTIVE_FAILURE_DEMOTION_THRESHOLD) == EXHAUSTED_TTL_DEMOTED_SECONDS

    def test_demotion_above_threshold(self):
        assert _exhausted_ttl(403, 10) == EXHAUSTED_TTL_DEMOTED_SECONDS

    def test_demotion_ignores_error_code(self):
        """Once demoted, ANY error code gets the long cooldown."""
        assert _exhausted_ttl(402, 5) == EXHAUSTED_TTL_DEMOTED_SECONDS
        assert _exhausted_ttl(404, 5) == EXHAUSTED_TTL_DEMOTED_SECONDS

    def test_401_not_affected_below_threshold(self):
        from agent.credential_pool import EXHAUSTED_TTL_401_SECONDS
        assert _exhausted_ttl(401, 0) == EXHAUSTED_TTL_401_SECONDS


class TestConsecutiveFailuresHelpers:
    def test_get_default_zero(self):
        e = _make_entry()
        assert _get_consecutive_failures(e) == 0

    def test_get_existing(self):
        e = _make_entry(cf=3)
        assert _get_consecutive_failures(e) == 3

    def test_get_garbage_returns_zero(self):
        e = _make_entry()
        e.extra["consecutive_failures"] = "not-a-number"
        assert _get_consecutive_failures(e) == 0

    def test_bump_same_code_increments(self):
        e = _make_entry(error_code=403, cf=2)
        result = _bump_consecutive_failures(e, 403)
        assert result == 3
        assert e.extra["consecutive_failures"] == 3

    def test_bump_different_code_resets(self):
        e = _make_entry(error_code=403, cf=5)
        result = _bump_consecutive_failures(e, 402)
        assert result == 1
        assert e.extra["consecutive_failures"] == 1

    def test_bump_first_time(self):
        e = _make_entry(error_code=None, cf=0)
        result = _bump_consecutive_failures(e, 403)
        assert result == 1

    def test_reset_clears(self):
        e = _make_entry(cf=3)
        _reset_consecutive_failures(e)
        assert "consecutive_failures" not in e.extra
        assert _get_consecutive_failures(e) == 0

    def test_reset_when_absent(self):
        e = _make_entry()
        _reset_consecutive_failures(e)  # should not raise
        assert _get_consecutive_failures(e) == 0


class TestDemotionSurvivesPersistence:
    """consecutive_failures is stored in extra — it round-trips through
    to_dict()/from_dict() because it's in _EXTRA_KEYS."""

    def test_round_trip(self):
        e = _make_entry(cf=3)
        d = e.to_dict()
        assert d.get("consecutive_failures") == 3

        restored = PooledCredential.from_dict("custom", d)
        assert _get_consecutive_failures(restored) == 3
