"""A stale owned handle must never acquire a recycled process or its descendants."""
import signal
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from tools.environments import local


@pytest.fixture
def cleanup(monkeypatch):
    proc = SimpleNamespace(pid=500, _hermes_pgid=500,
                           _hermes_process_identity=(500, 1000000),
                           kill=MagicMock(), poll=lambda: None, wait=MagicMock())
    child = MagicMock(pid=501)
    child.is_running.return_value = True
    parent = MagicMock()
    parent.children.return_value = [child]
    lookup, signals, retry = MagicMock(return_value=parent), MagicMock(), MagicMock()
    monkeypatch.setattr("psutil.Process", lookup)
    monkeypatch.setattr("gateway.status.get_process_start_time", lambda pid: 1000000)
    monkeypatch.setattr(local.os, "getpgid", lambda pid: 500 if pid == 500 else 777)
    monkeypatch.setattr(local.os, "getpgrp", lambda: 999)
    monkeypatch.setattr(local.os, "killpg", signals)
    monkeypatch.setattr(local, "_wait_for_group_exit", lambda *args: True)
    monkeypatch.setattr("agent.deadline.kill_process_tree", retry)
    return proc, child, lookup, signals, retry


def _refused(cleanup, monkeypatch, caplog, entrypoint, reason):
    proc, child, lookup, signals, retry = cleanup
    if reason == "missing":
        del proc._hermes_process_identity
    elif reason == "invalid":
        proc._hermes_process_identity = (500, "invalid")
    elif reason == "different_pid":
        proc._hermes_process_identity = (499, 1000000)
    elif reason == "different_group":
        monkeypatch.setattr(local.os, "getpgid", lambda pid: 1234)
    else:
        def identity(pid):
            if reason == "inaccessible":
                raise PermissionError("synthetic private detail")
            return None if reason == "unreadable" else 9000000
        monkeypatch.setattr("gateway.status.get_process_start_time", identity)
    getattr(object.__new__(local.LocalEnvironment), entrypoint)(proc)
    lookup.assert_not_called()
    signals.assert_not_called()
    proc.kill.assert_not_called()
    child.kill.assert_not_called()
    retry.assert_not_called()
    assert "cleanup incomplete" in caplog.text
    assert "synthetic private detail" not in caplog.text


@pytest.mark.linux_only
@pytest.mark.parametrize("entrypoint", ["_kill_process", "_force_kill_process", "_kill_spawned_tree"])
@pytest.mark.parametrize("reason", ["reused", "missing", "invalid", "different_pid", "different_group",
                                    "inaccessible", "unreadable"])
def test_unverified_handle_never_discovers_or_signals(cleanup, monkeypatch, caplog, entrypoint, reason):
    _refused(cleanup, monkeypatch, caplog, entrypoint, reason)


@pytest.mark.macos_only
@pytest.mark.parametrize("entrypoint", ["_kill_process", "_force_kill_process", "_kill_spawned_tree"])
@pytest.mark.parametrize("reason", ["reused", "missing", "invalid", "different_pid", "different_group",
                                    "inaccessible", "unreadable"])
def test_unverified_handle_never_discovers_or_signals_macos(cleanup, monkeypatch, caplog, entrypoint, reason):
    _refused(cleanup, monkeypatch, caplog, entrypoint, reason)


def _owned(cleanup, monkeypatch, caplog, scenario):
    proc, child, lookup, signals, retry = cleanup
    env = object.__new__(local.LocalEnvironment)
    if scenario == "shared_group":
        proc._hermes_pgid = 999
        monkeypatch.setattr(local.os, "getpgid", lambda pid: 999)
        env._kill_process(proc)
        signals.assert_not_called()
        proc.kill.assert_called_once_with()
        child.kill.assert_called_once_with()
    elif scenario == "force_shared_group":
        proc._hermes_pgid = 999
        monkeypatch.setattr(local.os, "getpgid", lambda pid: 999)
        env._force_kill_process(proc)
        signals.assert_not_called()
        proc.kill.assert_called_once_with()
        child.kill.assert_called_once_with()
    elif scenario == "force":
        env._force_kill_process(proc)
        signals.assert_called_once_with(500, signal.SIGKILL)  # windows-footgun: ok — native POSIX matrix only
        lookup.assert_not_called()
    elif scenario in {"grace_reuse", "grace_missing", "permission_reuse", "snapshot_reuse"}:
        def lose_identity(*args):
            monkeypatch.setattr("gateway.status.get_process_start_time",
                                lambda pid: None if scenario == "grace_missing" else 9000000)
            return False
        if scenario == "permission_reuse":
            def denied(*args):
                lose_identity()
                raise PermissionError("synthetic private detail")
            signals.side_effect = denied
        elif scenario == "snapshot_reuse":
            def replaced(*args, **kwargs):
                lose_identity()
                return [child]
            lookup.return_value.children.side_effect = replaced
        else:
            monkeypatch.setattr(local, "_wait_for_group_exit", lose_identity)
        env._kill_process(proc)
        assert signals.call_count == (0 if scenario == "snapshot_reuse" else 1)
        proc.kill.assert_not_called()
        if scenario == "snapshot_reuse":
            child.kill.assert_not_called()
        else:
            child.kill.assert_called_once_with()
        assert "cleanup incomplete" in caplog.text
    elif scenario == "spawn_reuse":
        new = SimpleNamespace(pid=500, poll=lambda: None, kill=MagicMock())
        current = [1000000]
        monkeypatch.setattr("gateway.status.get_process_start_time", lambda pid: current[0])
        monkeypatch.setattr(local.subprocess, "Popen", MagicMock(side_effect=[proc, new]))
        monkeypatch.setattr(local, "_find_bash", lambda: "/synthetic/bash")
        monkeypatch.setattr(local, "_make_run_env", lambda env: {})
        monkeypatch.setattr(env, "_recover_cwd", lambda: None)
        env.cwd, env.env = "/synthetic", {}
        old = env._run_bash("old")
        current[0] = 9000000
        newer = env._run_bash("new")
        assert old._hermes_process_identity != newer._hermes_process_identity
        env._kill_process(old)
        signals.assert_not_called()
        lookup.assert_not_called()
        env._kill_process(newer)
        signals.assert_called_once_with(500, signal.SIGTERM)
    else:
        if scenario == "drift":
            monkeypatch.setattr("gateway.status.get_process_start_time", lambda pid: 1000100)
        if scenario == "oserror":
            signals.side_effect = OSError("synthetic private detail")
        env._kill_spawned_tree(proc)
        signals.assert_called_once_with(500, signal.SIGTERM)
        if scenario == "oserror":
            proc.kill.assert_not_called()
            assert "cleanup incomplete" in caplog.text
        else:
            child.kill.assert_called_once_with()
    retry.assert_not_called()
    assert "synthetic private detail" not in caplog.text


_OWNED_CASES = ["normal", "force", "shared_group", "force_shared_group", "grace_reuse", "grace_missing",
                "permission_reuse", "snapshot_reuse", "spawn_reuse", "drift", "oserror"]


@pytest.mark.linux_only
@pytest.mark.parametrize("scenario", _OWNED_CASES)
def test_owned_cleanup_preserves_identity_across_paths(cleanup, monkeypatch, caplog, scenario):
    _owned(cleanup, monkeypatch, caplog, scenario)


@pytest.mark.macos_only
@pytest.mark.parametrize("scenario", _OWNED_CASES)
def test_owned_cleanup_preserves_identity_across_paths_macos(cleanup, monkeypatch, caplog, scenario):
    _owned(cleanup, monkeypatch, caplog, scenario)


@pytest.mark.windows_only
def test_windows_cleanup_preserves_existing_dispatch(monkeypatch):
    inherited, windows_kill = MagicMock(), MagicMock()
    monkeypatch.setattr(local.BaseEnvironment, "_kill_spawned_tree", inherited)
    monkeypatch.setattr(local, "_kill_process_windows", windows_kill)
    env = object.__new__(local.LocalEnvironment)
    proc = object()
    env._kill_spawned_tree(proc)
    inherited.assert_called_once_with(proc)
    env._kill_process(proc)
    env._force_kill_process(proc)
    assert windows_kill.call_args_list == [((proc,),), ((proc,),)]


@pytest.mark.macos_only
@pytest.mark.live_system_guard_bypass
@pytest.mark.parametrize("shared_group", [False, True])
@pytest.mark.parametrize("entrypoint", ["_kill_process", "_force_kill_process"])
def test_native_owned_process_cleanup_macos(tmp_path, monkeypatch, shared_group, entrypoint):
    """Validate real Darwin fingerprints/groups and teardown of only test-owned children."""
    import shlex
    import subprocess
    import sys

    env = object.__new__(local.LocalEnvironment)
    env.cwd, env.env = str(tmp_path), {}
    script = "import time; print('ready', flush=True); time.sleep(30)"
    if shared_group:
        proc = subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, text=True)
        local._capture_process_identity(proc)
    else:
        proc = env._run_bash(f"exec {shlex.quote(sys.executable)} -c {shlex.quote(script)}")
    try:
        assert proc.stdout.readline().strip() == "ready"
        assert proc._hermes_process_identity[0] == proc.pid
        assert local._owned_process_group(proc) == local.os.getpgid(proc.pid)
        if shared_group:
            assert proc._hermes_pgid == local.os.getpgrp()
            monkeypatch.setattr(local.os, "killpg", lambda *args: pytest.fail("signalled caller group"))
        else:
            assert proc._hermes_pgid == proc.pid != local.os.getpgrp()
        getattr(env, entrypoint)(proc)
        assert proc.wait(timeout=10) is not None
    finally:
        # This Popen is exclusively our test child; never discover or signal unrelated PIDs.
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=10)
        proc.stdout.close()
