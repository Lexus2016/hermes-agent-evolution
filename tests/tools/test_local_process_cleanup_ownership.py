"""Local cleanup never discovers or signals descendants of an unverified leader."""
import signal
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from tools.environments import local


@pytest.fixture
def cleanup(monkeypatch):
    child = MagicMock(pid=501)
    child.is_running.return_value = True
    parent = MagicMock()
    parent.children.return_value = [child]
    lookup = MagicMock(return_value=parent)
    signals = MagicMock()
    monkeypatch.setattr("psutil.Process", lookup)
    monkeypatch.setattr(local.os, "getpgid", lambda pid: 500 if pid == 500 else 777)
    monkeypatch.setattr(local.os, "getpgrp", lambda: 999)
    monkeypatch.setattr(local.os, "killpg", signals)
    monkeypatch.setattr(local, "_wait_for_group_exit", lambda *args: True)
    proc = SimpleNamespace(pid=500, _hermes_pgid=500, _hermes_pgid_start=1000000)
    return proc, child, lookup, signals


@pytest.mark.platforms("posix")
def test_recycled_leader_does_not_discover_or_kill_foreign_escapee(cleanup, monkeypatch):
    proc, child, lookup, signals = cleanup
    monkeypatch.setattr("gateway.status.get_process_start_time", lambda pid: 9000000)
    local._kill_process_group_posix(proc)
    lookup.assert_not_called()
    child.kill.assert_not_called()
    signals.assert_not_called()


@pytest.mark.platforms("posix")
def test_verified_owned_leader_keeps_group_and_escapee_cleanup(cleanup, monkeypatch):
    proc, child, lookup, signals = cleanup
    monkeypatch.setattr("gateway.status.get_process_start_time", lambda pid: proc._hermes_pgid_start)
    local._kill_process_group_posix(proc)
    lookup.assert_called_once_with(proc.pid)
    signals.assert_called_once_with(500, signal.SIGTERM)
    child.kill.assert_called_once_with()


@pytest.mark.parametrize("reason", ["missing_baseline", "unreadable", "inaccessible", "vanished", "invalid"])
@pytest.mark.platforms("posix")
def test_unproven_leader_identity_never_signals(cleanup, monkeypatch, reason):
    proc, child, lookup, signals = cleanup
    if reason == "missing_baseline":
        proc._hermes_pgid_start = None
    def identity(pid):
        if reason == "inaccessible":
            raise PermissionError("synthetic inaccessible identity")
        return None if reason in {"unreadable", "vanished"} else proc._hermes_pgid_start
    monkeypatch.setattr("gateway.status.get_process_start_time", identity)
    if reason == "vanished":
        def vanished(pid):
            raise ProcessLookupError(pid)
        monkeypatch.setattr(local.os, "getpgid", vanished)
    if reason == "invalid":
        def invalid(*args):
            raise ValueError("synthetic invalid identity")
        monkeypatch.setattr("gateway.status.start_time_fingerprints_match", invalid)
    local._kill_process_group_posix(proc)
    lookup.assert_not_called()
    signals.assert_not_called()
    child.kill.assert_not_called()


@pytest.mark.parametrize("owned", [False, True])
@pytest.mark.platforms("posix")
def test_timeout_backstop_keeps_ownership_guard(cleanup, monkeypatch, caplog, owned):
    proc, child, lookup, signals = cleanup
    monkeypatch.setattr("gateway.status.get_process_start_time",
                        lambda pid: proc._hermes_pgid_start if owned else 9000000)
    raw_retry = MagicMock()
    monkeypatch.setattr("agent.deadline.kill_process_tree", raw_retry)
    env = object.__new__(local.LocalEnvironment)
    env._kill_spawned_tree(proc)
    raw_retry.assert_not_called()
    if owned:
        signals.assert_called_once_with(500, signal.SIGTERM)
        child.kill.assert_called_once_with()
        assert "cleanup incomplete" not in caplog.text
    else:
        lookup.assert_not_called()
        signals.assert_not_called()
        child.kill.assert_not_called()
        assert "cleanup incomplete" in caplog.text


@pytest.mark.platforms("windows")
def test_windows_backstop_retains_existing_dispatch(monkeypatch):
    inherited = MagicMock()
    monkeypatch.setattr(local.BaseEnvironment, "_kill_spawned_tree", inherited)
    env = object.__new__(local.LocalEnvironment)
    proc = object()
    env._kill_spawned_tree(proc)
    inherited.assert_called_once_with(proc)


@pytest.mark.platforms("posix")
@pytest.mark.parametrize("entrypoint", ["_kill_process", "_force_kill_process", "_kill_spawned_tree"])
@pytest.mark.parametrize("failure", ["mismatch", "lookup_error"])
def test_refused_callers_never_fall_back_to_handle_signal(cleanup, monkeypatch, caplog, entrypoint, failure):
    proc, child, lookup, signals = cleanup
    proc.kill = MagicMock()
    monkeypatch.setattr("gateway.status.get_process_start_time", lambda pid: 9000000)
    if failure == "lookup_error":
        del proc._hermes_pgid
        def denied(pid):
            raise PermissionError("synthetic private detail")
        monkeypatch.setattr(local.os, "getpgid", denied)
    env = object.__new__(local.LocalEnvironment)
    getattr(env, entrypoint)(proc)
    proc.kill.assert_not_called()
    lookup.assert_not_called()
    signals.assert_not_called()
    child.kill.assert_not_called()
    assert "cleanup incomplete" in caplog.text
    assert "synthetic private detail" not in caplog.text


@pytest.mark.platforms("posix")
def test_force_cleanup_signals_verified_owned_group(cleanup, monkeypatch):
    proc, child, lookup, signals = cleanup
    proc.kill = MagicMock()
    monkeypatch.setattr("gateway.status.get_process_start_time", lambda pid: proc._hermes_pgid_start)
    env = object.__new__(local.LocalEnvironment)
    env._force_kill_process(proc)
    signals.assert_called_once_with(500, signal.SIGKILL)
    proc.kill.assert_called_once_with()
    lookup.assert_not_called()
    child.kill.assert_not_called()
