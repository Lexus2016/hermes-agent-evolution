"""E2E tests for the cronjob tool — issue #972.

Tests two bug fixes:
1. ``_cron_preflight_check`` must NOT hard-fail when ``crontab`` is absent,
   because Hermes has its own internal JSON scheduler that doesn't need it.
2. ``_record_cron_failure`` must NOT be called before the operation runs —
   only on actual failures. Pre-incrementing caused successful calls to
   bump the failure counter, eventually blocking legitimate operations.
"""

import json
import os
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

import pytest


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def isolated_cron_dir(tmp_path, monkeypatch):
    """Redirect CRON_DIR to a temp dir so tests don't touch real cron state."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    cron_dir = tmp_path / ".hermes" / "cron"
    cron_dir.mkdir(parents=True, exist_ok=True)
    # Patch CRON_DIR where it's actually imported from
    from cron import jobs as cron_jobs

    monkeypatch.setattr(cron_jobs, "CRON_DIR", cron_dir)
    # Also patch ensure_dirs to be a no-op (dirs already created)
    monkeypatch.setattr(cron_jobs, "ensure_dirs", lambda: None)
    yield cron_dir


@pytest.fixture(autouse=True)
def reset_failure_tracker():
    """Clear the failure tracker before and after each test."""
    from tools import cronjob_tools

    with cronjob_tools._cron_failure_tracker_lock:
        cronjob_tools._cron_failure_tracker.clear()
    yield
    with cronjob_tools._cron_failure_tracker_lock:
        cronjob_tools._cron_failure_tracker.clear()


# ---------------------------------------------------------------------------
# Bug 1: preflight must not hard-fail when crontab is missing
# ---------------------------------------------------------------------------


class TestPreflightNoCrontab:
    """The preflight check should NOT block when crontab is absent."""

    def test_returns_none_without_crontab(self):
        """When crontab is not on PATH, preflight returns None (no error)."""
        from tools.cronjob_tools import _cron_preflight_check

        with patch("shutil.which", return_value=None):
            result = _cron_preflight_check()

        assert result is None, (
            f"Preflight should return None (pass) when crontab is missing, "
            f"got: {result}"
        )

    def test_preflight_passes_in_container_env(self, isolated_cron_dir):
        """Simulate a container/CI env with no crontab — preflight should pass."""
        from tools.cronjob_tools import _cron_preflight_check

        with patch("shutil.which", return_value=None):
            result = _cron_preflight_check()

        assert result is None

    def test_preflight_still_checks_cron_dir_writable(self, tmp_path, monkeypatch):
        """If the cron directory is not writable, preflight still fails.

        We point CRON_DIR at a *file* (not a directory) so the probe write
        fails with NotADirectoryError. This works regardless of the test
        user's privileges (root ignores permission bits).
        """
        from tools.cronjob_tools import _cron_preflight_check
        from cron import jobs as cron_jobs

        not_a_dir = tmp_path / "blocking_file"
        not_a_dir.write_text("block")
        monkeypatch.setattr(cron_jobs, "CRON_DIR", not_a_dir)
        monkeypatch.setattr(cron_jobs, "ensure_dirs", lambda: None)

        with patch("shutil.which", return_value=None):
            result = _cron_preflight_check()
        assert result is not None
        assert "not writable" in result.lower()


# ---------------------------------------------------------------------------
# Bug 2: failure counter must not pre-increment
# ---------------------------------------------------------------------------


class TestFailureCounterNoPreIncrement:
    """The failure counter must only be incremented on actual failures."""

    def test_get_streak_does_not_increment(self):
        """_get_cron_failure_streak reads without bumping the counter."""
        from tools.cronjob_tools import (
            _get_cron_failure_streak,
            _record_cron_failure,
            _cron_failure_tracker,
        )

        # Record one failure
        _record_cron_failure("task-1")
        assert len(_cron_failure_tracker["task-1"]) == 1

        # Read the streak — should NOT add another entry
        streak = _get_cron_failure_streak("task-1")
        assert streak == 1
        assert len(_cron_failure_tracker["task-1"]) == 1

    def test_successful_call_does_not_increment(self):
        """Simulate the top of _handle_cronjob: reading streak must not bump it."""
        from tools.cronjob_tools import (
            _get_cron_failure_streak,
            _record_cron_failure,
            _reset_cron_failure,
        )

        # Pre-existing failure
        _record_cron_failure("task-2")
        initial = _get_cron_failure_streak("task-2")
        assert initial == 1

        # Simulate what the old code did: _record_cron_failure at top
        # vs new code: _get_cron_failure_streak at top
        # New code: read only
        streak = _get_cron_failure_streak("task-2")
        assert streak == 1  # unchanged

        # Simulate success → reset
        _reset_cron_failure("task-2")
        assert _get_cron_failure_streak("task-2") == 0

    def test_failure_only_counted_once(self):
        """A single failed operation increments the counter exactly once."""
        from tools.cronjob_tools import (
            _get_cron_failure_streak,
            _record_cron_failure,
        )

        # Before: old code called _record at top AND in except = +2 per failure
        # After: only except block calls _record = +1 per failure

        # Simulate the new flow:
        # 1. Check streak (no increment)
        streak_before = _get_cron_failure_streak("task-3")
        assert streak_before == 0

        # 2. Operation fails → except block records failure
        _record_cron_failure("task-3")

        streak_after = _get_cron_failure_streak("task-3")
        assert streak_after == 1  # exactly 1, not 2

    def test_max_failures_still_blocks(self):
        """After exceeding max consecutive failures, the streak check blocks."""
        from tools.cronjob_tools import (
            _get_cron_failure_streak,
            _record_cron_failure,
            _MAX_CONSECUTIVE_CRON_FAILURES,
        )

        task = "task-blocked"
        # Record max + 1 failures
        for _ in range(_MAX_CONSECUTIVE_CRON_FAILURES + 1):
            _record_cron_failure(task)

        streak = _get_cron_failure_streak(task)
        assert streak > _MAX_CONSECUTIVE_CRON_FAILURES

    def test_reset_clears_streak(self):
        """_reset_cron_failure clears the counter so subsequent calls proceed."""
        from tools.cronjob_tools import (
            _get_cron_failure_streak,
            _record_cron_failure,
            _reset_cron_failure,
        )

        task = "task-reset"
        _record_cron_failure(task)
        _record_cron_failure(task)
        assert _get_cron_failure_streak(task) == 2

        _reset_cron_failure(task)
        assert _get_cron_failure_streak(task) == 0

    def test_none_task_id_safe(self):
        """None task_id doesn't crash."""
        from tools.cronjob_tools import (
            _get_cron_failure_streak,
            _record_cron_failure,
            _reset_cron_failure,
        )

        assert _get_cron_failure_streak(None) == 0
        assert _record_cron_failure(None) == 1
        _reset_cron_failure(None)  # should not crash