"""Tests for provider-agnostic connection/read timeout backoff (#1093)."""

import pytest
from agent.retry_utils import (
    adaptive_timeout_backoff,
    jittered_backoff,
    _TIMEOUT_LONG_BACKOFF,
    _TIMEOUT_SHORT_ATTEMPTS,
)


class TestAdaptiveTimeoutBackoff:
    """Verify the two-tier timeout backoff schedule."""

    def test_short_attempts_use_default_wait(self):
        """First ``short_attempts`` retries pass through the default wait."""
        for attempt in range(1, _TIMEOUT_SHORT_ATTEMPTS + 1):
            wait, label = adaptive_timeout_backoff(attempt, default_wait=5.0)
            assert wait == 5.0
            assert label == "timeout_short"

    def test_long_attempts_use_backoff_table(self):
        """After short attempts, waits walk the long-backoff table with jitter."""
        for i, expected_base in enumerate(_TIMEOUT_LONG_BACKOFF):
            attempt = _TIMEOUT_SHORT_ATTEMPTS + 1 + i
            wait, label = adaptive_timeout_backoff(attempt, default_wait=5.0)
            assert label == "timeout_long"
            # Wait should be in [base, base * 1.2] (jitter_ratio=0.2).
            assert expected_base <= wait <= expected_base * 1.2

    def test_long_attempts_clamped_to_last_entry(self):
        """Attempts beyond the table length clamp to the last (longest) entry."""
        last_base = _TIMEOUT_LONG_BACKOFF[-1]
        wait, label = adaptive_timeout_backoff(100, default_wait=5.0)
        assert label == "timeout_long"
        assert last_base <= wait <= last_base * 1.2

    def test_custom_short_attempts(self):
        """Custom short_attempts shifts the tier boundary."""
        wait, label = adaptive_timeout_backoff(1, default_wait=3.0, short_attempts=5)
        assert wait == 3.0
        assert label == "timeout_short"
        wait, label = adaptive_timeout_backoff(6, default_wait=3.0, short_attempts=5)
        assert label == "timeout_long"

    def test_schedule_is_gentler_than_overload(self):
        """The timeout schedule must not exceed 60s — the timeout deadline
        itself already imposes a long per-attempt delay, so we deliberately
        avoid stacking overload's very long 90/120s tail on top."""
        assert max(_TIMEOUT_LONG_BACKOFF) <= 60.0
