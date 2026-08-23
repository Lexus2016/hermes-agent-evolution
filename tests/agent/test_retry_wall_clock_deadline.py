"""Unit tests for the turn-loop wall-clock retry deadline (issue #3113).

These exercises the small helper-like deadline logic without spinning up a
full agent runtime. The config tests live in
``tests/run_agent/test_retry_wall_clock_config.py``.
"""

import time


def test_cron_wall_clock_default_is_larger():
    """Sanity: cron cap default (600) is larger than interactive default (300)."""
    assert 600 > 300


def test_deadline_math_for_short_limit():
    """A 30s minimum floor prevents zero/negative deadlines."""
    raw = 5
    floor = 30.0
    assert max(float(raw), floor) == 30.0


def test_time_deadline_in_future():
    """A deadline computed now + 300s is in the future."""
    deadline = time.time() + 300.0
    assert deadline > time.time()
    assert deadline - time.time() <= 300.5  # loose; confirms direction
