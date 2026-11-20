"""Tests for issue #1504: the ``_cron_pool.submit()`` call inside
``_run_job_impl`` must catch the
``RuntimeError: cannot schedule new futures after interpreter shutdown``
race during Python interpreter teardown, log it at debug level, and
return a graceful failure tuple — instead of propagating an uncaught
error that spawns spurious failure records.

The guard mirrors the existing ``_submit_with_guard`` pattern already
used for the parallel dispatch pool: only the interpreter-shutdown
RuntimeError is swallowed and returns a clean failure tuple; any other
RuntimeError re-raises from the guard and is then handled by the outer
``except Exception`` in ``_run_job_impl``, which converts it to a failure
tuple with a different error message.
"""

import concurrent.futures
import inspect
from unittest.mock import MagicMock, patch

import pytest

import cron.scheduler as sched


class TestCronPoolShutdownGuard:
    """The submit guard inside _run_job_impl must gracefully handle the
    interpreter-shutdown RuntimeError and distinguish it from other errors."""

    def _make_agent_path_job(self) -> dict:
        """Minimal job dict that takes the agent (non-no_agent) path."""
        return {
            "id": "test-shutdown-guard",
            "name": "shutdown-guard-test",
            "prompt": "hello world",
        }

    def _common_patches(self, cron_pool, job):
        """Build patch context that lets run_job reach the ``_cron_pool.submit`` site.

        v0.23.1 creates more than one executor on this path (the session store's,
        and the persistent ``cron-parallel`` pool), so identify the agent pool by
        what it is instead of by creation order: the session store is stubbed out
        and any ``cron-parallel`` executor is a plain stand-in.
        """
        real_pool_cls = concurrent.futures.ThreadPoolExecutor

        def pool_factory(*args, **kwargs):
            # Only the agent executor created inside cron/scheduler.py is replaced;
            # every other executor (session store, the persistent cron-parallel pool,
            # mock internals) stays a real one so nothing else sees a fake future.
            # side_effect runs inside unittest.mock, so the creation site is found
            # by walking a few frames rather than by the immediate caller.
            frames = inspect.stack()[1:8]
            from_scheduler = bool(
                not kwargs.get("thread_name_prefix")
                and any(f.filename.endswith("cron/scheduler.py") for f in frames)
            )
            if from_scheduler:
                return cron_pool
            return real_pool_cls(*args, **kwargs)

        tpe_patch = patch(
            "concurrent.futures.ThreadPoolExecutor",
            side_effect=pool_factory,
        )
        # Own the fire in this process: an unowned agent fire is handed to the
        # gateway (which v0.23.1 will try to start) instead of running here.
        owner_patch = patch(
            "gateway.session_cron.current_execution",
            return_value=(MagicMock(), "pool-test-session", job["id"], None),
        )
        session_db_patch = patch(
            "cron.scheduler._open_cron_session_db", return_value=MagicMock()
        )
        cfg_patch = patch("cron.scheduler.load_config", return_value={})
        runtime_patch = patch(
            "hermes_cli.runtime_provider.resolve_runtime_provider",
            return_value={
                "provider": "openrouter",
                "model": "test-model",
                "api_key": "fake-key",
                "base_url": "https://example.com",
                "api_mode": "chat_completions",
            },
        )
        aiaagent_patch = patch("run_agent.AIAgent", return_value=MagicMock())
        mcp_patch = patch("tools.mcp_tool.discover_mcp_tools", return_value=[])
        return [tpe_patch, owner_patch, session_db_patch, cfg_patch, runtime_patch,
                aiaagent_patch, mcp_patch]

    def test_shutdown_runtimeerror_returns_clean_failure(self):
        """When _cron_pool.submit() raises the interpreter-shutdown
        RuntimeError, run_job returns (False, "", "", <error>) where the
        error message identifies the shutdown race — not a generic failure.
        The pool is shut down with cancel_futures=True."""
        cron_pool = MagicMock()
        cron_pool.submit.side_effect = RuntimeError(
            "cannot schedule new futures after interpreter shutdown"
        )

        job = self._make_agent_path_job()
        patches = self._common_patches(cron_pool, job)
        for p in patches:
            p.start()
        try:
            success, output, final_response, error = sched.run_job(job)
        finally:
            for p in patches:
                p.stop()

        assert success is False
        # The guard's return tuple carries the shutdown-specific message.
        assert "cannot schedule new futures" in (error or "")

        # The guard's shutdown call includes cancel_futures=True.
        shutdown_calls = cron_pool.shutdown.call_args_list
        assert any(
            call.kwargs.get("cancel_futures") is True
            and call.kwargs.get("wait") is False
            for call in shutdown_calls
        ), f"Expected shutdown(wait=False, cancel_futures=True); got {shutdown_calls}"

    def test_other_runtimeerror_does_not_match_shutdown(self):
        """When _cron_pool.submit() raises a *different* RuntimeError,
        the guard re-raises it (does not swallow it as a shutdown race).
        The outer except Exception in _run_job_impl catches it and returns
        a failure tuple whose error message does NOT contain the shutdown
        phrase — proving the guard correctly distinguished the two cases.
        """
        cron_pool = MagicMock()
        cron_pool.submit.side_effect = RuntimeError("something else entirely")

        job = self._make_agent_path_job()
        patches = self._common_patches(cron_pool, job)
        for p in patches:
            p.start()
        try:
            success, output, final_response, error = sched.run_job(job)
        finally:
            for p in patches:
                p.stop()

        assert success is False
        # The error must NOT carry the shutdown-specific message — the
        # guard did not swallow it as a shutdown race.
        assert "cannot schedule new futures" not in (error or "")
        # The original error content should appear somewhere in the output.
        assert "something else" in (error or output or "")