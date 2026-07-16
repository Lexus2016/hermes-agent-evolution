"""Tests for provider-agnostic overload backoff (#1040)."""

import pytest
from agent.retry_utils import (
    adaptive_overload_backoff,
    jittered_backoff,
    overload_retry_ceiling,
    _OVERLOAD_LONG_BACKOFF,
    _OVERLOAD_SHORT_ATTEMPTS,
)


class TestAdaptiveOverloadBackoff:
    """Verify the two-tier overload backoff schedule."""

    def test_short_attempts_use_default_wait(self):
        """First ``short_attempts`` retries pass through the default wait."""
        for attempt in range(1, _OVERLOAD_SHORT_ATTEMPTS + 1):
            wait, label = adaptive_overload_backoff(attempt, default_wait=5.0)
            assert wait == 5.0
            assert label == "overload_short"

    def test_long_attempts_use_backoff_table(self):
        """After short attempts, waits walk the long-backoff table."""
        for i, expected_base in enumerate(_OVERLOAD_LONG_BACKOFF):
            attempt = _OVERLOAD_SHORT_ATTEMPTS + 1 + i
            wait, label = adaptive_overload_backoff(attempt, default_wait=5.0)
            assert label == "overload_long"
            # Wait should be in [base, base * 1.2] (jitter_ratio=0.2)
            assert expected_base <= wait <= expected_base * 1.2

    def test_long_attempts_clamped_to_last_entry(self):
        """Attempts beyond the table length are clamped to the last entry."""
        last_base = _OVERLOAD_LONG_BACKOFF[-1]
        wait, label = adaptive_overload_backoff(100, default_wait=5.0)
        assert label == "overload_long"
        assert last_base <= wait <= last_base * 1.2

    def test_custom_short_attempts(self):
        """Custom short_attempts shifts the tier boundary."""
        wait, label = adaptive_overload_backoff(1, default_wait=3.0, short_attempts=5)
        assert wait == 3.0
        assert label == "overload_short"
        wait, label = adaptive_overload_backoff(6, default_wait=3.0, short_attempts=5)
        assert label == "overload_long"


class TestOverloadRetryCeiling:
    """Verify the ceiling calculation."""

    def test_ceiling_exceeds_table(self):
        """Ceiling must exceed the last long-tier index for every wait to execute."""
        ceiling = overload_retry_ceiling()
        assert ceiling == _OVERLOAD_SHORT_ATTEMPTS + len(_OVERLOAD_LONG_BACKOFF) + 1

    def test_custom_short_attempts_ceiling(self):
        """Ceiling scales with custom short_attempts."""
        ceiling = overload_retry_ceiling(short_attempts=4)
        assert ceiling == 4 + len(_OVERLOAD_LONG_BACKOFF) + 1

    def test_ceiling_allows_full_schedule(self):
        """Every long-tier entry must be reachable within the ceiling."""
        ceiling = overload_retry_ceiling()
        # The last long-tier attempt is at: short_attempts + len(table)
        last_long_attempt = _OVERLOAD_SHORT_ATTEMPTS + len(_OVERLOAD_LONG_BACKOFF)
        assert ceiling > last_long_attempt
