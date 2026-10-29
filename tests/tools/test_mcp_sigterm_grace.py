"""The stdio MCP reaper must give a child a real window to exit on SIGTERM before SIGKILL.

Same-day evidence (#3545): a restart of the context7 MCP server (an npx/Node child that closes its
own sockets on SIGTERM) was force-killed at 04:17:37 after the old flat 2 s sleep, and that force-kill
is what the operator saw as an unclean restart. The window is now 5 s and polled, so a prompt exit
costs nothing extra while a stubborn child still gets the full grace before escalation.
"""

import signal

import pytest

from tools import mcp_tool_lifecycle as lifecycle
from tools.mcp_tool import _lock

RECORDED = 1_700_000_000


def _reset_ledgers() -> None:
    with _lock:
        lifecycle._stdio_pids.clear()
        lifecycle._orphan_stdio_pids.clear()
        lifecycle._orphan_stdio_pid_servers.clear()
        lifecycle._stdio_pgids.clear()
        lifecycle._stdio_starttimes.clear()


def _stage_orphan(fake_pid: int, owner: str = "context7") -> None:
    _reset_ledgers()
    with _lock:
        lifecycle._orphan_stdio_pids.add(fake_pid)
        lifecycle._orphan_stdio_pid_servers[fake_pid] = owner
        lifecycle._stdio_pgids[fake_pid] = fake_pid
        lifecycle._stdio_starttimes[fake_pid] = RECORDED


def _record_signals(monkeypatch) -> list:
    signals = []

    def _fake_signal(pid, sig, server_name, pgid, my_pgid, expected_start=None):
        signals.append((pid, sig))

    monkeypatch.setattr(lifecycle, "_signal_mcp_process", _fake_signal)
    monkeypatch.setattr(lifecycle, "_leader_start_time", lambda pid: RECORDED)
    return signals


def _record_sleeps(monkeypatch) -> list:
    sleeps = []
    monkeypatch.setattr(lifecycle.time, "sleep", lambda seconds: sleeps.append(seconds))
    return sleeps


def test_grace_window_is_longer_than_the_old_flat_sleep():
    """Regression on the 2 s sleep that context7 overran: the window must be strictly larger."""
    assert lifecycle._SIGTERM_GRACE_SECONDS >= 5.0
    assert lifecycle._SIGTERM_GRACE_SECONDS > 2.0


def test_prompt_exit_skips_the_force_kill(monkeypatch, caplog):
    """A child that dies on SIGTERM is never SIGKILLed, and costs no waiting at all."""
    fake_pid = 606060
    _stage_orphan(fake_pid)
    signals = _record_signals(monkeypatch)
    sleeps = _record_sleeps(monkeypatch)
    monkeypatch.setattr("gateway.status._pid_exists", lambda pid: False)
    monkeypatch.setattr(lifecycle, "_group_alive", lambda pgid, my_pgid: False)

    with caplog.at_level("DEBUG", logger="tools.mcp_tool"):
        lifecycle._kill_orphaned_mcp_children()

    assert signals == [(fake_pid, signal.SIGTERM)]
    assert sleeps == [], "a prompt exit must not pay the grace window"
    assert not [r for r in caplog.records if "Force-killed" in r.getMessage()]


def test_stubborn_child_gets_the_whole_window_then_sigkill(monkeypatch, caplog):
    """A child that ignores SIGTERM is polled for the full window before the SIGKILL pass."""
    fake_pid = 616161
    _stage_orphan(fake_pid)
    signals = _record_signals(monkeypatch)
    sleeps = _record_sleeps(monkeypatch)
    monkeypatch.setattr("gateway.status._pid_exists", lambda pid: True)
    monkeypatch.setattr(lifecycle, "_group_alive", lambda pgid, my_pgid: False)

    with caplog.at_level("WARNING", logger="tools.mcp_tool"):
        lifecycle._kill_orphaned_mcp_children()

    expected_polls = round(lifecycle._SIGTERM_GRACE_SECONDS / lifecycle._TERM_POLL_INTERVAL)
    assert sleeps == [lifecycle._TERM_POLL_INTERVAL] * expected_polls
    assert signals == [(fake_pid, signal.SIGTERM), (fake_pid, signal.SIGKILL)]
    killed = [r.getMessage() for r in caplog.records if "Force-killed" in r.getMessage()]
    assert killed and "5.0s SIGTERM grace window" in killed[0]


def test_wait_returns_as_soon_as_the_group_disappears(monkeypatch):
    """Mid-window exit ends the wait early: only the polls actually needed are charged."""
    fake_pid = 626262
    pids = {fake_pid: "lazyweb"}
    pgids = {fake_pid: fake_pid}
    alive_reads = {"n": 2}  # initial probe + one poll report the child alive

    def _pid_exists(pid):
        alive_reads["n"] -= 1
        return alive_reads["n"] >= 0

    monkeypatch.setattr("gateway.status._pid_exists", _pid_exists)
    monkeypatch.setattr(lifecycle, "_group_alive", lambda pgid, my_pgid: False)
    sleeps = _record_sleeps(monkeypatch)

    assert lifecycle._wait_for_graceful_exit(pids, pgids, None) == []
    assert len(sleeps) == 2


def test_no_pids_means_no_wait(monkeypatch):
    sleeps = _record_sleeps(monkeypatch)
    assert lifecycle._wait_for_graceful_exit({}, {}, None) == []
    assert sleeps == []


def test_reapable_pids_respect_server_name(monkeypatch):
    """The per-server sweep (a stdio reconnect) must not touch another server's child."""
    _reset_ledgers()
    signals = _record_signals(monkeypatch)
    sleeps = _record_sleeps(monkeypatch)
    monkeypatch.setattr("gateway.status._pid_exists", lambda pid: False)
    monkeypatch.setattr(lifecycle, "_group_alive", lambda pgid, my_pgid: False)
    with _lock:
        for pid, owner in ((707070, "context7"), (717171, "lazyweb")):
            lifecycle._orphan_stdio_pids.add(pid)
            lifecycle._orphan_stdio_pid_servers[pid] = owner
            lifecycle._stdio_pgids[pid] = pid
            lifecycle._stdio_starttimes[pid] = RECORDED

    lifecycle._kill_orphaned_mcp_children(server_name="context7")

    assert signals == [(707070, signal.SIGTERM)]
    assert sleeps == []
    with _lock:
        assert 717171 in lifecycle._orphan_stdio_pids


@pytest.fixture(autouse=True)
def _clean_ledgers():
    _reset_ledgers()
    yield
    _reset_ledgers()
