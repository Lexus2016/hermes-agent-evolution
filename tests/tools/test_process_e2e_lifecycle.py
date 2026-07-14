"""E2E tests for the process tool lifecycle — issue #971.

Exercises ``_handle_process`` (the real tool dispatch) against actual
background processes started via ``ProcessRegistry.spawn_local``.
Prior fix #781 addressed schema validation and action-name handling,
and #890 added typo suggestions + session_id auto-fill.  These tests
verify the full lifecycle (start → poll → wait → kill) end-to-end so
regressions in the dispatch layer are caught before production.
"""

import json
import sys
import time

import pytest

from tools.process_registry import ProcessRegistry, _handle_process


@pytest.fixture()
def registry():
    """Create a fresh ProcessRegistry with a real local process."""
    r = ProcessRegistry()
    yield r
    # Cleanup: kill any leftover processes
    for p in r.list_sessions():
        if isinstance(p, dict) and p.get("status") != "exited":
            try:
                r.kill_process(p["id"])
            except Exception:
                pass


# ---------------------------------------------------------------------------
# We need _handle_process to use our fixture registry, not the module-global
# one.  The handler references ``process_registry`` at call time, so patching
# the module attribute works.
# ---------------------------------------------------------------------------


@pytest.fixture()
def handler(registry, monkeypatch):
    """Return _handle_process patched to use our fixture registry."""
    import tools.process_registry as mod

    monkeypatch.setattr(mod, "process_registry", registry)
    return _handle_process


class TestProcessLifecycleE2E:
    """Full lifecycle: start a real process, then poll/wait/kill via the
    tool handler dispatch."""

    def test_poll_running_process(self, handler, registry):
        """poll on a running process returns status + output."""
        session = registry.spawn_local(
            f"{sys.executable} -c 'import time; time.sleep(5)'",
            task_id="e2e-poll",
        )
        try:
            time.sleep(0.3)  # let it start
            result = json.loads(handler({"action": "poll", "session_id": session.id}))
            assert "status" in result
            # Process should be running or recently started
            assert result.get("status") in ("running", "exited")
        finally:
            registry.kill_process(session.id)

    def test_wait_short_lived_process_exits(self, handler, registry):
        """wait on a short-lived process returns status=exited with exit_code."""
        session = registry.spawn_local(
            f"{sys.executable} -c 'print(\"done\"); exit(0)'",
            task_id="e2e-wait-exit",
        )
        result = json.loads(
            handler({"action": "wait", "session_id": session.id, "timeout": 10})
        )
        assert result["status"] == "exited"
        assert result["exit_code"] == 0
        assert "done" in result.get("output", "")

    def test_wait_timeout_on_long_process(self, handler, registry):
        """wait with a short timeout returns status=timeout when the process
        is still running."""
        session = registry.spawn_local(
            f"{sys.executable} -c 'import time; time.sleep(30)'",
            task_id="e2e-wait-timeout",
        )
        try:
            result = json.loads(
                handler({"action": "wait", "session_id": session.id, "timeout": 2})
            )
            assert result["status"] == "timeout"
            assert "timeout_note" in result
        finally:
            registry.kill_process(session.id)

    def test_kill_terminates_running_process(self, handler, registry):
        """kill on a running process terminates it and returns success."""
        session = registry.spawn_local(
            f"{sys.executable} -c 'import time; time.sleep(30)'",
            task_id="e2e-kill",
        )
        time.sleep(0.3)  # let it start
        result = json.loads(handler({"action": "kill", "session_id": session.id}))
        assert result.get("status") in ("killed", "exited")
        # Verify it's no longer active
        poll = json.loads(handler({"action": "poll", "session_id": session.id}))
        assert poll["status"] == "exited"

    def test_log_returns_output(self, handler, registry):
        """log on a completed process returns its output."""
        session = registry.spawn_local(
            f"{sys.executable} -c 'print(\"hello world\"); exit(0)'",
            task_id="e2e-log",
        )
        # Wait for it to finish
        json.loads(handler({"action": "wait", "session_id": session.id, "timeout": 10}))
        result = json.loads(handler({"action": "log", "session_id": session.id}))
        assert "output" in result
        assert "hello world" in result["output"]

    def test_list_shows_started_process(self, handler, registry):
        """list includes a process we just started."""
        session = registry.spawn_local(
            f"{sys.executable} -c 'import time; time.sleep(5)'",
            task_id="e2e-list",
        )
        try:
            result = json.loads(handler({"action": "list"}, task_id="e2e-list"))
            ids = [p.get("session_id") for p in result.get("processes", [])]
            assert session.id in ids
        finally:
            registry.kill_process(session.id)


class TestProcessSessionIdCoercion:
    """session_id coercion (int → str) — issue #971 mentions missing session_id
    as a failure shape. Verify int session_ids are handled."""

    def test_integer_session_id_coerced(self, handler, registry):
        """An integer session_id doesn't crash — it's coerced to string."""
        # Start a real process
        session = registry.spawn_local(
            f"{sys.executable} -c 'print(\"ok\"); exit(0)'",
            task_id="e2e-int-sid",
        )
        try:
            # The handler coerces session_id to str; a non-matching int
            # should give not_found, not a crash
            result = json.loads(
                handler({"action": "poll", "session_id": 99999})
            )
            # Should get a not_found error, not an exception
            assert "status" in result or "error" in result
        finally:
            registry.kill_process(session.id)


class TestProcessAutoFillE2E:
    """E2E test for session_id auto-fill when exactly one process exists."""

    def test_auto_fill_poll_single_process(self, handler, registry):
        """When exactly one active process exists and session_id is omitted,
        auto-fill kicks in and poll succeeds.

        This is a regression test for the bug fixed in this PR: the auto-fill
        code was reading ``p.get("id")`` from ``list_sessions`` results, but
        ``list_sessions`` returns entries keyed as ``"session_id"``, so the
        auto-fill always resolved to an empty string and failed with
        ``session_id is required``.
        """
        session = registry.spawn_local(
            f"{sys.executable} -c 'import time; time.sleep(5)'",
            task_id="e2e-autofill",
        )
        try:
            time.sleep(0.3)  # let it start
            # poll without session_id — should auto-fill to our single process
            result = json.loads(handler({"action": "poll"}, task_id="e2e-autofill"))
            assert "status" in result
            assert result["status"] in ("running", "exited")
        finally:
            registry.kill_process(session.id)