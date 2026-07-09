"""Regression tests for the cronjob tool failure tracker (#827).

The failure tracker must:
  1. Only record failures when an actual exception occurs (not on every call).
  2. Cap consecutive failures at _MAX_CONSECUTIVE_CRON_FAILURES with a clear
     diagnostic appended to the error message.
  3. Reset on success so transient failures don't permanently block a task.
  4. Skip the preflight probe for read-only 'list' action.
"""

import json
import os
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

# Ensure the repo root is importable
_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


@pytest.fixture(autouse=True)
def _isolate_hermes_home(tmp_path, monkeypatch):
    """Redirect HERMES_HOME so tests never touch real config."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    # Clear any stale failure tracker state between tests
    from tools import cronjob_tools as ct
    ct._cron_failure_tracker.clear()
    yield
    ct._cron_failure_tracker.clear()


@pytest.fixture
def _restore_preflight():
    """Restore _cron_preflight_check after each test (in case it was patched)."""
    yield
    # No restoration needed — patch handles cleanup.


def test_successful_calls_do_not_record_failures():
    """A successful cronjob(action='list') must NOT increment the failure tracker."""
    from tools import cronjob_tools as ct

    # Bypass preflight — we're testing the tracker, not cron availability.
    with patch.object(ct, "_cron_preflight_check", return_value=None):
        with patch.object(ct, "list_jobs", return_value=[]):
            result = ct.cronjob(action="list", task_id="test-task")

    data = json.loads(result)
    assert data["success"] is True
    # No failures should have been recorded for a successful call.
    assert "test-task" not in ct._cron_failure_tracker


def test_failure_tracker_increments_on_exception():
    """When cronjob raises an exception, the failure tracker increments."""
    from tools import cronjob_tools as ct

    with patch.object(ct, "_cron_preflight_check", return_value=None):
        with patch.object(ct, "create_job", side_effect=RuntimeError("boom")):
            result = ct.cronjob(
                action="create",
                schedule="30m",
                prompt="test",
                task_id="fail-task",
            )

    # The tracker should have recorded exactly 1 failure.
    assert len(ct._cron_failure_tracker.get("fail-task", [])) == 1
    # The result should be an error.
    assert "failed" in result.lower() or "error" in result.lower()


def test_failure_streak_appends_diagnostic_message():
    """After _MAX_CONSECUTIVE_CRON_FAILURES, a diagnostic suffix is appended."""
    from tools import cronjob_tools as ct

    task_id = "streak-task"
    # Pre-seed the tracker to the limit using current monotonic timestamps
    # so they're not filtered out by _CRON_RETRY_WINDOW_SECONDS.
    import time as _time
    now = _time.monotonic()
    ct._cron_failure_tracker[task_id] = [now, now, now]  # 3 prior failures

    with patch.object(ct, "_cron_preflight_check", return_value=None):
        with patch.object(ct, "create_job", side_effect=RuntimeError("boom")):
            result = ct.cronjob(
                action="create",
                schedule="30m",
                prompt="test",
                task_id=task_id,
            )

    # The error should include the "consecutive times" diagnostic.
    assert "consecutive times" in result
    assert "rejected until the streak expires" in result


def test_reset_on_success_clears_streak():
    """A successful call after failures resets the streak."""
    from tools import cronjob_tools as ct

    task_id = "reset-task"
    # Pre-seed a failure streak.
    ct._cron_failure_tracker[task_id] = [1.0, 2.0]

    with patch.object(ct, "_cron_preflight_check", return_value=None):
        with patch.object(ct, "list_jobs", return_value=[]):
            result = ct.cronjob(action="list", task_id=task_id)

    data = json.loads(result)
    assert data["success"] is True
    # The streak should have been reset.
    assert task_id not in ct._cron_failure_tracker


def test_list_action_skips_preflight():
    """The 'list' action must NOT call _cron_preflight_check."""
    from tools import cronjob_tools as ct

    preflight_mock = MagicMock(return_value="cron not available")
    with patch.object(ct, "_cron_preflight_check", preflight_mock):
        with patch.object(ct, "list_jobs", return_value=[]):
            result = ct.cronjob(action="list", task_id="preflight-skip")

    data = json.loads(result)
    # list should succeed even though preflight would have failed.
    assert data["success"] is True
    # Preflight should NOT have been called for 'list'.
    preflight_mock.assert_not_called()


def test_mutating_action_calls_preflight():
    """Non-list actions MUST call _cron_preflight_check."""
    from tools import cronjob_tools as ct

    preflight_mock = MagicMock(return_value=None)
    with patch.object(ct, "_cron_preflight_check", preflight_mock):
        with patch.object(ct, "create_job", return_value={
            "id": "j1", "name": "test", "schedule_display": "30m",
            "next_run_at": None, "deliver": "local",
            "skills": [], "skill": None,
        }) as mock_create:
            ct.cronjob(
                action="create",
                schedule="30m",
                prompt="test",
                task_id="preflight-test",
            )

    preflight_mock.assert_called_once()


def test_preflight_error_blocks_mutating_action():
    """When preflight fails for a mutating action, the tool returns the error."""
    from tools import cronjob_tools as ct

    preflight_msg = "The 'crontab' command is not available on PATH."
    with patch.object(ct, "_cron_preflight_check", return_value=preflight_msg):
        result = ct.cronjob(
            action="create",
            schedule="30m",
            prompt="test",
            task_id="preflight-fail",
        )

    assert preflight_msg in result
    data = json.loads(result)
    assert data.get("success") is False
    assert preflight_msg in data.get("error", "")


def test_no_task_id_still_works():
    """When task_id is None, failure tracking gracefully degrades."""
    from tools import cronjob_tools as ct

    with patch.object(ct, "_cron_preflight_check", return_value=None):
        with patch.object(ct, "list_jobs", return_value=[]):
            result = ct.cronjob(action="list", task_id=None)  # type: ignore[arg-type]

    data = json.loads(result)
    assert data["success"] is True


if __name__ == "__main__":
    pytest.main([__file__, "-v"])