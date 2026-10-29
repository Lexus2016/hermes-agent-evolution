"""A single transient keepalive failure must not tear the MCP server down (#3545).

Same-day evidence: at 08:03:56 the lazyweb keepalive failed once with ``MCPError: server returned an
error response`` and the server flipped connected -> degraded, dropping its registered tools and
rebuilding the transport for a hiccup the server itself answered. One failure is now retried once
inside the keepalive cadence; only a second consecutive failure degrades the session.
"""

import asyncio
import logging

import pytest

from tools.mcp_tool import MCPServerTask


class _ProbeTask(MCPServerTask):
    """Server whose keepalive probe fails ``failures`` times, then succeeds."""

    def __init__(self, name: str, failures: int) -> None:
        super().__init__(name)
        self._failures = failures
        self.probe_calls = 0

    async def _keepalive_probe(self) -> None:
        self.probe_calls += 1
        if self.probe_calls <= self._failures:
            raise RuntimeError("server returned an error response")


def _drive(task: MCPServerTask, timeout_cycles: int):
    """Run ``_wait_for_lifecycle_event`` through ``timeout_cycles`` keepalive timeouts, then shut down.

    ``asyncio.sleep`` is stubbed so the retry delay is instant, and the loop's ``asyncio.wait``
    reports a timeout for the first ``timeout_cycles`` cycles instead of waiting the real interval.
    """
    real_wait = asyncio.wait
    state = {"n": 0}

    async def _fake_wait(tasks, timeout=None, return_when=None):
        state["n"] += 1
        if state["n"] <= timeout_cycles:
            return set(), set(tasks)
        task._shutdown_event.set()
        return await real_wait(tasks, timeout=0.5, return_when=return_when or asyncio.FIRST_COMPLETED)

    async def _no_sleep(_seconds):
        return None

    async def _scenario():
        original_wait, original_sleep = asyncio.wait, asyncio.sleep
        asyncio.wait, asyncio.sleep = _fake_wait, _no_sleep
        try:
            return await task._wait_for_lifecycle_event()
        finally:
            asyncio.wait, asyncio.sleep = original_wait, original_sleep

    return asyncio.run(_scenario())


def _task(failures: int) -> _ProbeTask:
    task = _ProbeTask("lazyweb", failures)
    task._config = {"url": "https://example.test/mcp", "keepalive_interval": 5}
    task.session = object()
    return task


def test_one_failed_keepalive_is_retried_and_keeps_the_session(caplog):
    task = _task(failures=1)

    with caplog.at_level(logging.INFO, logger="tools.mcp_tool"):
        reason = _drive(task, timeout_cycles=1)

    assert reason == "shutdown", "one transient failure must not trigger a reconnect"
    assert task.probe_calls == 2
    assert not task._reconnect_event.is_set()
    assert task._suspect_reason is None
    messages = [r.getMessage() for r in caplog.records]
    assert any("retrying once" in m for m in messages), messages
    assert any("keepalive recovered on retry" in m for m in messages), messages
    assert not any("keepalive failed twice" in m for m in messages), messages


def test_two_consecutive_failures_still_degrade_the_session(caplog):
    task = _task(failures=2)

    with caplog.at_level(logging.WARNING, logger="tools.mcp_tool"):
        reason = _drive(task, timeout_cycles=1)

    assert reason == "reconnect"
    assert task.probe_calls == 2
    messages = [r.getMessage() for r in caplog.records]
    assert any("keepalive failed twice" in m and "connected \u2192 degraded" in m for m in messages), messages
    assert task._suspect_reason and "keepalive failed twice" in task._suspect_reason


def test_retry_delay_stays_inside_the_keepalive_cadence():
    """The retry must not push the next probe past the server's keepalive interval."""
    from tools import mcp_tool

    assert 0 < mcp_tool._KEEPALIVE_RETRY_DELAY <= mcp_tool._DEFAULT_KEEPALIVE_INTERVAL
    task = _task(failures=1)
    delays = []
    real_wait = asyncio.wait
    state = {"n": 0}

    async def _fake_wait(tasks, timeout=None, return_when=None):
        state["n"] += 1
        if state["n"] == 1:
            return set(), set(tasks)
        task._shutdown_event.set()
        return await real_wait(tasks, timeout=0.5, return_when=return_when or asyncio.FIRST_COMPLETED)

    async def _record_sleep(seconds):
        delays.append(seconds)

    async def _scenario():
        original_wait, original_sleep = asyncio.wait, asyncio.sleep
        asyncio.wait, asyncio.sleep = _fake_wait, _record_sleep
        try:
            await task._wait_for_lifecycle_event()
        finally:
            asyncio.wait, asyncio.sleep = original_wait, original_sleep

    asyncio.run(_scenario())

    assert delays == [min(mcp_tool._KEEPALIVE_RETRY_DELAY, 5)]


@pytest.mark.no_isolate
def test_shutdown_during_the_retry_wins(monkeypatch, tmp_path):
    """A shutdown that lands inside the retry delay must end the loop, not reconnect."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    task = _task(failures=1)

    async def _scenario():
        return await task._wait_for_lifecycle_event()

    state = {"n": 0}

    async def _fake_wait(tasks, timeout=None, return_when=None):
        state["n"] += 1
        return set(), set(tasks)

    async def _shutdown_sleep(_seconds):
        task._shutdown_event.set()

    original_wait, original_sleep = asyncio.wait, asyncio.sleep
    asyncio.wait, asyncio.sleep = _fake_wait, _shutdown_sleep
    try:
        reason = asyncio.run(_scenario())
    finally:
        asyncio.wait, asyncio.sleep = original_wait, original_sleep

    assert reason == "shutdown"
    assert not task._reconnect_event.is_set()
    assert task.probe_calls == 1, "no retry probe may run after shutdown was requested"
