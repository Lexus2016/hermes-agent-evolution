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
    proc = SimpleNamespace(pid=500, _hermes_pgid=500, _hermes_pgid_start=1000000,
                           _hermes_process_identity=(500, 1000000), kill=MagicMock())
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
        proc._hermes_process_identity = None
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
    proc.kill.assert_not_called()
    lookup.assert_not_called()
    child.kill.assert_not_called()


@pytest.mark.platforms("posix")
@pytest.mark.parametrize("entrypoint", ["_kill_process", "_kill_spawned_tree", "_force_kill_process"])
def test_shared_caller_group_uses_wrapper_identity(cleanup, monkeypatch, entrypoint):
    proc, child, lookup, signals = cleanup
    proc._hermes_pgid = 999
    proc._hermes_process_identity = (proc.pid, proc._hermes_pgid_start)
    proc.kill = MagicMock()
    monkeypatch.setattr(local.os, "getpgid", lambda pid: 999)
    monkeypatch.setattr("gateway.status.get_process_start_time",
                        lambda pid: 1000000 if pid == proc.pid else 2000000)
    env = object.__new__(local.LocalEnvironment)
    getattr(env, entrypoint)(proc)
    signals.assert_not_called()
    lookup.assert_called_once_with(proc.pid)
    lookup.return_value.kill.assert_not_called()
    child.kill.assert_called_once_with()
    proc.kill.assert_called_once_with()


@pytest.mark.platforms("posix")
@pytest.mark.parametrize("entrypoint", ["_kill_process", "_kill_spawned_tree", "_force_kill_process"])
@pytest.mark.parametrize("current", [None, 9000000])
def test_shared_group_unverified_wrapper_never_discovers_children(cleanup, monkeypatch, entrypoint, current):
    proc, child, lookup, signals = cleanup
    proc._hermes_pgid = 999
    proc._hermes_process_identity = (proc.pid, proc._hermes_pgid_start)
    proc.kill = MagicMock()
    monkeypatch.setattr(local.os, "getpgid", lambda pid: 999)
    monkeypatch.setattr("gateway.status.get_process_start_time", lambda pid: current)
    env = object.__new__(local.LocalEnvironment)
    getattr(env, entrypoint)(proc)
    lookup.assert_not_called()
    signals.assert_not_called()
    proc.kill.assert_not_called()
    child.kill.assert_not_called()


@pytest.mark.platforms("posix")
@pytest.mark.parametrize("change", ["missing_capture", "different_pid", "replacement_during_discovery"])
def test_shared_wrapper_capture_cannot_authorize_another_identity(cleanup, monkeypatch, change):
    proc, child, lookup, signals = cleanup
    proc._hermes_pgid = 999
    proc._hermes_process_identity = (proc.pid, proc._hermes_pgid_start)
    proc.kill = MagicMock()
    monkeypatch.setattr(local.os, "getpgid", lambda pid: 999)
    starts = iter([1000000, 9000000] if change == "replacement_during_discovery" else [1000000])
    monkeypatch.setattr("gateway.status.get_process_start_time", lambda pid: next(starts))
    if change == "missing_capture":
        del proc._hermes_process_identity
    elif change == "different_pid":
        proc._hermes_process_identity = (123, proc._hermes_pgid_start)
    local._kill_process_group_posix(proc)
    signals.assert_not_called()
    proc.kill.assert_not_called()
    lookup.return_value.kill.assert_not_called()
    child.kill.assert_not_called()
    if change != "replacement_during_discovery":
        lookup.assert_not_called()


@pytest.mark.platforms("posix")
def test_spawn_captures_wrapper_identity_even_in_shared_group(monkeypatch):
    proc = SimpleNamespace(pid=500, poll=lambda: None)
    monkeypatch.setattr(local.subprocess, "Popen", lambda *args, **kwargs: proc)
    monkeypatch.setattr(local, "_find_bash", lambda: "/synthetic/bash")
    monkeypatch.setattr(local, "_make_run_env", lambda env: {})
    monkeypatch.setattr(local.os, "getpgid", lambda pid: 999)
    monkeypatch.setattr("gateway.status.get_process_start_time", lambda pid: 1000000 if pid == 500 else 2000000)
    env = object.__new__(local.LocalEnvironment)
    env.env, env.cwd = {}, "/synthetic"
    env._recover_cwd = lambda: None
    assert env._run_bash("true") is proc
    assert proc._hermes_pgid == 999
    assert proc._hermes_process_identity == (500, 1000000)


@pytest.mark.platforms("posix")
@pytest.mark.parametrize("scenario", ["snapshot_reuse", "grace_reuse", "grace_missing", "permission_reuse"])
def test_external_cleanup_revalidates_before_each_signal(cleanup, monkeypatch, caplog, scenario):
    proc, child, lookup, signals = cleanup
    monkeypatch.setattr("gateway.status.get_process_start_time", lambda pid: 1000000)
    def lose_identity(*args, **kwargs):
        monkeypatch.setattr("gateway.status.get_process_start_time",
                            lambda pid: None if scenario == "grace_missing" else 9000000)
        return False
    if scenario == "snapshot_reuse":
        def replaced(*args, **kwargs):
            lose_identity()
            return [child]
        lookup.return_value.children.side_effect = replaced
    elif scenario == "permission_reuse":
        def denied(*args):
            lose_identity()
            raise PermissionError("synthetic private detail")
        signals.side_effect = denied
    else:
        monkeypatch.setattr(local, "_wait_for_group_exit", lose_identity)
    local._kill_process_group_posix(proc)
    assert signals.call_count == (0 if scenario == "snapshot_reuse" else 1)
    proc.kill.assert_not_called()
    if scenario == "snapshot_reuse":
        child.kill.assert_not_called()
    else:
        child.kill.assert_called_once_with()
    assert "cleanup incomplete" in caplog.text
    assert "synthetic private detail" not in caplog.text


@pytest.mark.platforms("posix")
@pytest.mark.parametrize("exited", [False, True])
def test_capture_reaped_or_reused_handle_never_borrows_new_ownership(cleanup, monkeypatch, exited):
    proc, child, lookup, signals = cleanup
    proc.poll = lambda: 0 if exited else None
    current = [1000000]
    monkeypatch.setattr("gateway.status.get_process_start_time", lambda pid: current[0])
    local._capture_process_identity(proc)
    newer = SimpleNamespace(pid=proc.pid, poll=lambda: None, kill=MagicMock())
    current[0] = 9000000
    local._capture_process_identity(newer)
    assert proc._hermes_process_identity != newer._hermes_process_identity
    if exited:
        assert proc._hermes_process_identity is None
    local._kill_process_group_posix(proc)
    signals.assert_not_called()
    lookup.assert_not_called()
    proc.kill.assert_not_called()
    local._kill_process_group_posix(newer)
    signals.assert_called_once_with(500, signal.SIGTERM)
    child.kill.assert_called_once_with()


@pytest.mark.platforms("posix")
def test_lost_leader_does_not_signal_reused_captured_descendant(cleanup, monkeypatch):
    proc, child, lookup, signals = cleanup
    monkeypatch.setattr("gateway.status.get_process_start_time", lambda pid: 1000000)
    def lose_both_identities(*args):
        monkeypatch.setattr("gateway.status.get_process_start_time", lambda pid: 9000000)
        child.is_running.return_value = False
        return False
    monkeypatch.setattr(local, "_wait_for_group_exit", lose_both_identities)
    local._kill_process_group_posix(proc)
    signals.assert_called_once_with(500, signal.SIGTERM)
    lookup.assert_called_once_with(proc.pid)
    proc.kill.assert_not_called()
    child.kill.assert_not_called()
