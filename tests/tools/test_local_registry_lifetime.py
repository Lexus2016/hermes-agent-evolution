"""Adopted foreground commands retain the registry's graceful tree-kill contract."""

import contextlib
import json
import shlex
import sys
import threading
import time

import psutil
import pytest

from tools.environments import local
from tools import process_registry as pr
from tests.waiting import wait_until


_CHILD = """
import os
from pathlib import Path
import signal
import sys
import time

root = Path(sys.argv[1])
if sys.argv[2] == 'escaped_natural_finish':
    os.setsid()

def term(*_):
    target = root / 'child.term'
    if not target.exists():
        target.write_text(str(time.monotonic()))

signal.signal(signal.SIGTERM, term)
(root / 'child.pid').write_text(str(os.getpid()))
(root / 'child.ready').touch()
while not (root / 'child.finish').exists():
    time.sleep(0.01)
"""

_PARENT = """
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

root, mode = Path(sys.argv[1]), sys.argv[2]
child = subprocess.Popen([sys.executable, str(root / 'child.py'), str(root), mode],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

def term(*_):
    target = root / 'parent.term'
    if not target.exists():
        target.write_text(str(time.monotonic()))
    if mode == 'self_managing':
        # Longer than the old owner's fixed 0.5s grace, within our configured 2s.
        time.sleep(0.8)
        if child.poll() is not None or (root / 'child.term').exists():
            (root / 'outcome').write_text('child was signalled before parent cleanup')
            sys.exit(7)
        child.kill()
        child.wait(timeout=10)
        (root / 'outcome').write_text('parent reaped its untouched child')
        sys.exit(0)
    if mode in {'parent_exits', 'natural_finish', 'escaped_natural_finish'}:
        sys.exit(0)

signal.signal(signal.SIGTERM, term)
deadline = time.monotonic() + 20
while not (root / 'child.ready').exists():
    if time.monotonic() >= deadline:
        raise RuntimeError('child did not become ready')
    time.sleep(0.01)
(root / 'parent.pid').write_text(str(os.getpid()))
(root / 'ready').touch()
while True:
    time.sleep(0.01)
"""


def _wait_until(predicate, description, timeout=15):
    wait_until(predicate, timeout=timeout, what=description)


def _alive(proc):
    try:
        return proc.is_running() and proc.status() != psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return False


def _retained(registry, session, proc, child, checkpoint):
    assert _alive(child), "zero grace must not escalate to killing the captured child"
    assert proc._owner.poll() is None, "incomplete cleanup lost its lifetime owner"
    assert not session.exited, "reader published completion while an owned child survived"
    assert not session._completion_event.is_set()
    listed = {item["session_id"]: item for item in registry.list_sessions()}
    assert listed[session.id]["status"] == "running"
    assert registry.poll(session.id)["status"] == "running"
    assert registry.get(session.id) is session
    assert any(row["session_id"] == session.id for row in json.loads(checkpoint.read_text()))


def _contract(tmp_path, monkeypatch, case):
    home = tmp_path / "home"
    profile = home / ".hermes"
    profile.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("HERMES_HOME", str(profile))
    checkpoint = profile / "processes.json"
    monkeypatch.setattr(pr, "CHECKPOINT_PATH", checkpoint)
    registry = pr.ProcessRegistry()
    grace = [2.0 if case in {"self_managing", "two_grace_windows"} else 0.0]
    monkeypatch.setattr(registry, "_daemon_term_grace_seconds", lambda: grace[0])

    # Observe the real reader's EOF/wait path without replacing its behavior.
    reader_waiting = threading.Event()
    finish_reader = registry._finish_reader

    def observe_reader(*args, **kwargs):
        reader_waiting.set()
        return finish_reader(*args, **kwargs)

    monkeypatch.setattr(registry, "_finish_reader", observe_reader)
    (tmp_path / "child.py").write_text(_CHILD)
    (tmp_path / "parent.py").write_text(_PARENT)
    mode = "ignore" if case in {"two_grace_windows", "zero_grace_retry"} else case
    command = "exec " + " ".join(map(shlex.quote, [
        sys.executable, str(tmp_path / "parent.py"), str(tmp_path), mode]))
    env = object.__new__(local.LocalEnvironment)
    env.cwd, env.env = str(tmp_path), {}
    proc = session = None
    owned = []
    try:
        proc = env._run_bash(command)
        owned.extend([psutil.Process(proc.pid), psutil.Process(proc.command_pid)])
        _wait_until(lambda: (tmp_path / "child.ready").exists(), "child PID was not published")
        child = psutil.Process(int((tmp_path / "child.pid").read_text()))
        owned.append(child)
        _wait_until(lambda: (tmp_path / "ready").exists(), "parent/child signal handlers not ready")
        parent = owned[1]
        session = registry.adopt_local(proc, command="test-owned parent and child", cwd=str(tmp_path),
                                       notify_on_complete=False)
        started = time.monotonic()
        result = registry.kill_process(session.id)
        elapsed = time.monotonic() - started

        if case == "self_managing":
            assert result["status"] == "killed", result
            outcome = tmp_path / "outcome"
            assert outcome.exists(), "parent was killed before its configured shutdown grace elapsed"
            assert outcome.read_text() == "parent reaped its untouched child"
            assert not (tmp_path / "child.term").exists(), "registry signalled a self-managed child"
            assert proc.poll() == 0, "self-managing workload must exit cleanly"
        elif case == "two_grace_windows":
            assert result["status"] == "killed", result
            assert (tmp_path / "parent.term").exists()
            child_term = float((tmp_path / "child.term").read_text())
            assert child_term - started >= grace[0], "child received TERM during the parent's grace"
            assert elapsed >= 2 * grace[0], "force kill skipped one configured TERM grace window"
        else:
            assert result["status"] == "error" and result.get("process_running") is True, result
            if case in {"parent_exits", "natural_finish", "escaped_natural_finish"}:
                _wait_until(lambda: not _alive(parent), "TERM-responsive parent did not exit")
                assert reader_waiting.wait(10), "real registry reader never reached EOF/wait"
            else:
                assert _alive(parent)
            _retained(registry, session, proc, child, checkpoint)
            if case in {"natural_finish", "escaped_natural_finish"}:
                (tmp_path / "child.finish").touch()
                assert session._completion_event.wait(15), "completed captured tree remained stuck running"
                assert registry.poll(session.id)["status"] == "exited"
            else:
                grace[0] = 0.2
                retry = registry.kill_process(session.id)
                assert retry["status"] == "killed", retry

        _wait_until(lambda: all(not _alive(p) for p in owned), "test-owned tree survived completed kill")
        assert session._completion_event.wait(10)
        assert session.exited
        assert not any(row["session_id"] == session.id for row in json.loads(checkpoint.read_text()))
    finally:
        # Identity-aware handles captured at spawn; never discover unrelated processes at teardown.
        for target in reversed(owned):
            with contextlib.suppress(psutil.NoSuchProcess, psutil.AccessDenied):
                if _alive(target):
                    target.kill()
        if proc is not None:
            with contextlib.suppress(Exception):
                proc._owner.wait(timeout=5)
            if session is not None and session._reader_thread is not None:
                session._reader_thread.join(5)
            proc.release()
            for stream in (proc.stdin, proc.stdout, proc.stderr):
                if stream is not None:
                    with contextlib.suppress(OSError, ValueError):
                        stream.close()


_CASES = ["self_managing", "two_grace_windows", "zero_grace_retry", "parent_exits", "natural_finish",
          "escaped_natural_finish"]


@pytest.mark.platforms("linux")
@pytest.mark.live_system_guard_bypass
@pytest.mark.parametrize("case", _CASES)
def test_adopted_registry_lifetime_linux(tmp_path, monkeypatch, case):
    _contract(tmp_path, monkeypatch, case)


@pytest.mark.platforms("macos")
@pytest.mark.live_system_guard_bypass
@pytest.mark.parametrize("case", _CASES)
def test_adopted_registry_lifetime_macos(tmp_path, monkeypatch, case):
    _contract(tmp_path, monkeypatch, case)
