"""Cron delegation drain barrier tests (issue #1200).

Covers ``wait_for_session_delegations`` — the drain step the cron scheduler
runs after the agent's ``run_conversation`` returns (or the inactivity
timeout fires) but before the cron thread pool is shut down.

Without this barrier, background subagents dispatched via
``delegate_task(background=true)`` are still running on the daemon executor
when the cron session tears down, orphaning their completion events and
losing artifacts.
"""

import threading
import time
from unittest.mock import MagicMock, patch

import pytest

import tools.async_delegation as ad


@pytest.fixture(autouse=True)
def _reset_async_delegation():
    ad._reset_for_tests()
    yield
    ad._reset_for_tests()


class TestWaitForSessionDelegations:
    """Behavioral contracts for wait_for_session_delegations()."""

    def _seed_record(
        self,
        delegation_id,
        parent_session_id="cron_job1_20260721_120000",
        status="running",
    ):
        """Seed a delegation record directly into the in-memory registry."""
        with ad._records_lock:
            ad._records[delegation_id] = {
                "delegation_id": delegation_id,
                "status": status,
                "parent_session_id": parent_session_id,
                "session_key": "",
                "origin_ui_session_id": "",
                "interrupt_fn": None,
                "goal": "test goal",
                "context": None,
                "toolsets": None,
                "role": "leaf",
                "model": None,
                "dispatched_at": time.time(),
                "completed_at": None,
            }

    def _complete_after(self, delegation_id, delay, lock):
        """Mark a record completed after *delay* seconds from a background thread."""
        def _completer():
            time.sleep(delay)
            with lock:
                if delegation_id in ad._records:
                    ad._records[delegation_id]["status"] = "completed"
                    ad._records[delegation_id]["completed_at"] = time.time()

        t = threading.Thread(target=_completer, daemon=True)
        t.start()
        return t

    def test_no_delegations_returns_immediately(self):
        """When there are zero running delegations for the session, drain is instant."""
        start = time.monotonic()
        result = ad.wait_for_session_delegations(
            parent_session_id="cron_nonexistent",
            deadline_seconds=10.0,
            poll_interval=0.1,
        )
        elapsed = time.monotonic() - start
        assert result == {
            "waited": 0,
            "completed": 0,
            "interrupted": 0,
            "timed_out": False,
        }
        # Should return in well under 1 second, not 10s.
        assert elapsed < 1.0

    def test_delegation_completes_before_deadline(self):
        """A delegation that finishes on its own is waited for, not interrupted."""
        self._seed_record("d1")
        lock = threading.Lock()
        self._complete_after("d1", delay=0.3, lock=lock)

        start = time.monotonic()
        result = ad.wait_for_session_delegations(
            parent_session_id="cron_job1_20260721_120000",
            deadline_seconds=10.0,
            poll_interval=0.1,
        )
        elapsed = time.monotonic() - start

        assert result["waited"] == 1
        assert result["completed"] == 1
        assert result["interrupted"] == 0
        assert result["timed_out"] is False
        # Should take ~0.3s (the completion delay), not 10s.
        assert elapsed < 5.0

    def test_deadline_expires_interrupts_remaining(self):
        """When the deadline expires, remaining delegations are force-interrupted."""
        self._seed_record("d1")
        self._seed_record("d2")

        with patch("tools.async_delegation.interrupt_for_session") as mock_int:
            mock_int.return_value = 2
            start = time.monotonic()
            result = ad.wait_for_session_delegations(
                parent_session_id="cron_job1_20260721_120000",
                deadline_seconds=0.5,
                poll_interval=0.1,
            )
            elapsed = time.monotonic() - start

        assert result["waited"] == 2
        assert result["timed_out"] is True
        assert result["interrupted"] == 2
        # interrupt_for_session must be called with the right parent_session_id
        kwargs = mock_int.call_args.kwargs
        assert kwargs["parent_session_id"] == "cron_job1_20260721_120000"
        # Should take ~0.5s (the deadline), not much longer.
        assert elapsed < 3.0

    def test_only_matches_own_session(self):
        """Delegations from a different parent_session_id are not drained."""
        self._seed_record("d1", parent_session_id="cron_job1_20260721_120000")
        self._seed_record("d2", parent_session_id="cron_other_job_999")

        result = ad.wait_for_session_delegations(
            parent_session_id="cron_job1_20260721_120000",
            deadline_seconds=0.3,
            poll_interval=0.1,
        )

        # Only d1 is counted; d2 belongs to a different session.
        assert result["waited"] == 1
        # d1 never completes, so deadline fires and d1 is interrupted.
        assert result["timed_out"] is True

    def test_completed_records_not_counted(self):
        """Already-completed delegations are not waited for."""
        self._seed_record("d1", status="completed")
        self._seed_record("d2", status="running")

        with patch("tools.async_delegation.interrupt_for_session") as mock_int:
            mock_int.return_value = 1
            result = ad.wait_for_session_delegations(
                parent_session_id="cron_job1_20260721_120000",
                deadline_seconds=0.3,
                poll_interval=0.1,
            )

        # Only d2 (running) is counted.
        assert result["waited"] == 1
        assert result["timed_out"] is True
        mock_int.assert_called_once()

    def test_on_interrupt_callback_invoked(self):
        """The on_interrupt callback is called when delegations are force-interrupted."""
        self._seed_record("d1")
        callback = MagicMock()

        with patch("tools.async_delegation.interrupt_for_session") as mock_int:
            mock_int.return_value = 1
            ad.wait_for_session_delegations(
                parent_session_id="cron_job1_20260721_120000",
                deadline_seconds=0.2,
                poll_interval=0.05,
                on_interrupt=callback,
            )

        callback.assert_called_once_with(1)

    def test_on_interrupt_exception_swallowed(self):
        """An exception in on_interrupt must not propagate."""
        self._seed_record("d1")
        callback = MagicMock(side_effect=RuntimeError("boom"))

        with patch("tools.async_delegation.interrupt_for_session") as mock_int:
            mock_int.return_value = 1
            # Must not raise.
            result = ad.wait_for_session_delegations(
                parent_session_id="cron_job1_20260721_120000",
                deadline_seconds=0.2,
                poll_interval=0.05,
                on_interrupt=callback,
            )

        assert result["interrupted"] == 1
        callback.assert_called_once()

    def test_mixed_completion_and_timeout(self):
        """Some delegations finish, others time out — both paths are exercised."""
        self._seed_record("d1")
        self._seed_record("d2")
        lock = threading.Lock()
        # d1 completes quickly; d2 never does.
        self._complete_after("d1", delay=0.2, lock=lock)

        with patch("tools.async_delegation.interrupt_for_session") as mock_int:
            mock_int.return_value = 1
            result = ad.wait_for_session_delegations(
                parent_session_id="cron_job1_20260721_120000",
                deadline_seconds=0.5,
                poll_interval=0.1,
            )

        # Both were running at call time.
        assert result["waited"] == 2
        assert result["timed_out"] is True
        # Only d2 was interrupted (d1 finished on its own).
        assert result["interrupted"] == 1


class TestCronConfigKeys:
    """Verify the config keys added for issue #1200 exist with correct defaults."""

    def test_delegation_drain_seconds_exists(self):
        from hermes_cli.config import DEFAULT_CONFIG

        cron_cfg = DEFAULT_CONFIG.get("cron", {})
        assert "delegation_drain_seconds" in cron_cfg
        assert cron_cfg["delegation_drain_seconds"] == 1200

    def test_inactivity_timeout_seconds_exists(self):
        from hermes_cli.config import DEFAULT_CONFIG

        cron_cfg = DEFAULT_CONFIG.get("cron", {})
        assert "inactivity_timeout_seconds" in cron_cfg
        assert cron_cfg["inactivity_timeout_seconds"] == 900