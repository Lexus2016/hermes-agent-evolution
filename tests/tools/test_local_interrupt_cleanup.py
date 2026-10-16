"""Regression tests for _wait_for_process subprocess cleanup on exception exit.

When the poll loop exits via KeyboardInterrupt or SystemExit (SIGTERM via
cli.py signal handler, SIGINT on the main thread in non-interactive -q mode,
or explicit sys.exit from some caller), the child subprocess must be killed
before the exception propagates — otherwise the local backend's use of
os.setsid leaves an orphan with PPID=1.

The live repro that motivated this: hermes chat -q ... 'sleep 300', SIGTERM
to the python process, sleep 300 survived with PPID=1 for the full 300 s
because _wait_for_process never got to call _kill_process before python
died.  See commit message for full context.
"""
import contextlib
import json
import os
import subprocess
import threading
import time
from types import SimpleNamespace

import pytest

from tools.environments.local import LocalEnvironment
from tests.waiting import wait_until


@pytest.fixture(autouse=True)
def _isolate_hermes_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "logs").mkdir(exist_ok=True)


def _pgid_still_alive(pgid: int) -> bool:
    """Return True if any process in the given process group is still alive."""
    try:
        os.killpg(pgid, 0)  # signal 0 = existence check
        return True
    except ProcessLookupError:
        return False


def _process_group_snapshot(pgid: int) -> str:
    """Return a process-table snapshot for diagnostics."""
    return subprocess.run(
        ["ps", "-o", "pid,ppid,pgid,stat,cmd", "-g", str(pgid)],
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()


def _wait_for_pgid_exit(pgid: int, timeout: float = 60.0) -> bool:
    """Wait for a process group to disappear under loaded CI hosts.

    The cleanup chain is: SIGTERM → 3s TimeoutStopSec → SIGKILL → reap.
    Under heavy parallel-test load (40 workers, 6-shard CI), the full
    sequence can exceed 10s. Default timeout is generous to avoid CI
    flakes; in practice the wait returns in <1s on quiet hosts.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _pgid_still_alive(pgid):
            return True
        time.sleep(0.1)
    return not _pgid_still_alive(pgid)


@pytest.mark.platforms("linux")
def test_kill_process_refuses_cached_pgid_without_live_ownership(monkeypatch, caplog):
    """A cached numeric group cannot authorize signals after its owner disappears."""
    env = object.__new__(LocalEnvironment)
    proc = SimpleNamespace(
        pid=12345,
        _hermes_pgid=67890,
        poll=lambda: 0,
        kill=lambda: None,
    )
    killpg_calls = []

    def fake_getpgid(_pid):
        raise ProcessLookupError

    def fake_killpg(pgid, sig):
        killpg_calls.append((pgid, sig))
        if sig == 0:
            raise ProcessLookupError

    monkeypatch.setattr(os, "getpgid", fake_getpgid)
    monkeypatch.setattr(os, "killpg", fake_killpg)

    env._kill_process(proc)

    assert killpg_calls == []
    assert "cleanup incomplete" in caplog.text


@pytest.mark.platforms("linux")
def test_wait_for_process_kills_subprocess_on_keyboardinterrupt(monkeypatch):
    """When KeyboardInterrupt arrives mid-poll, the subprocess group must be
    killed before the exception is re-raised."""
    env = LocalEnvironment(cwd="/tmp")
    try:
        result_holder = {}
        started, proc_holder = _observe_foreground_wait(env, monkeypatch)

        # Drive execute() on a separate thread so we can SIGNAL-interrupt it
        # via a thread-targeted exception without killing our test process.
        def worker():
            # Spawn a subprocess that will definitely be alive long enough
            # to observe the cleanup, via env.execute(...) — the normal path
            # that goes through _wait_for_process.
            try:
                result_holder["result"] = env.execute("sleep 30", timeout=60)
            except BaseException as e:
                result_holder["exception"] = type(e).__name__

        t = threading.Thread(target=worker, daemon=True)
        t.start()
        # Observe the actual published handle, never an argv substring during
        # helper startup before its private session has been established.
        assert started.wait(20), "foreground command never reached its wait"
        target_pid = proc_holder["proc"].pid
        pgid = os.getpgid(target_pid)
        assert _pgid_still_alive(pgid), "sanity: subprocess should be alive"

        # Now inject a KeyboardInterrupt into the worker thread the same
        # way CPython's signal machinery would.  We use ctypes.PyThreadState_SetAsyncExc
        # which is how signal delivery to non-main threads is simulated.
        import ctypes
        # py-thread-state exception targets need the ident, not the Thread
        tid = t.ident
        assert tid is not None
        # Fire KeyboardInterrupt into the worker thread
        ret = ctypes.pythonapi.PyThreadState_SetAsyncExc(
            ctypes.c_ulong(tid), ctypes.py_object(KeyboardInterrupt),
        )
        assert ret == 1, f"SetAsyncExc returned {ret}, expected 1"

        # Give the worker a moment to: hit the exception at the next poll,
        # run the except-block cleanup (_kill_process), and exit.  Under
        # heavy load the SIGTERM → 3s wait → SIGKILL chain can take longer
        # than 5s before the worker's join() returns; bumped to 15s.
        t.join(timeout=30.0)
        assert not t.is_alive(), "worker didn't exit within 30 s of the interrupt"

        # The critical assertion: the subprocess GROUP must be dead.  Not
        # just the bash wrapper — the 'sleep 30' child too. Under heavy load,
        # process-group disappearance can lag briefly after the worker exits,
        # especially if the process is already dying or waiting to be reaped.
        assert _wait_for_pgid_exit(pgid), (
            f"subprocess group {pgid} is STILL ALIVE after worker received "
            f"KeyboardInterrupt — orphan bug regressed.  This is the "
            f"sleep-300-survives-SIGTERM scenario from Physikal's Apr 2026 "
            f"report.  See tools/environments/base.py _wait_for_process "
            f"except-block.\n{_process_group_snapshot(pgid)}"
        )
        # And the worker should have observed the KeyboardInterrupt (i.e.
        # it re-raised cleanly, not silently swallowed).
        assert result_holder.get("exception") == "KeyboardInterrupt", (
            f"worker result: {result_holder!r} — expected KeyboardInterrupt "
            f"propagation after cleanup"
        )
    finally:
        try:
            env.cleanup()
        except Exception:
            pass


def _observe_foreground_wait(env, monkeypatch):
    started, captured = threading.Event(), {}
    wait = env._wait_for_process
    def observed(proc, *args, **kwargs):
        captured["proc"] = proc
        started.set()
        return wait(proc, *args, **kwargs)
    monkeypatch.setattr(env, "_wait_for_process", observed)
    return started, captured


@pytest.mark.platforms("linux")
def test_exit_cleanup_kills_foreground_command_still_running(monkeypatch):
    """The host-exit funnel (CLI, one-shot, messaging gateway, serve atexit) must take an
    in-flight foreground command's process group with it: it runs in its own session, so
    the host exiting mid-command would otherwise orphan it to init."""
    from tools import terminal_tool_lifecycle

    monkeypatch.setattr(terminal_tool_lifecycle, "_scratch_paths", list)
    env = LocalEnvironment(cwd="/tmp")
    result: dict = {}
    started, captured = _observe_foreground_wait(env, monkeypatch)
    proc = None
    t = threading.Thread(target=lambda: result.update(env.execute("sleep 3517", timeout=600)), daemon=True)
    try:
        t.start()
        assert started.wait(20), "foreground command never reached its wait"
        proc = captured["proc"]
        pgid = os.getpgid(proc.pid)

        terminal_tool_lifecycle.cleanup_all_environments()

        assert _wait_for_pgid_exit(pgid, timeout=15.0), (
            f"foreground command survived exit cleanup:\n{_process_group_snapshot(pgid)}")
        t.join(timeout=15.0)
        assert not t.is_alive() and result.get("returncode") not in (None, 0), result
    finally:
        if proc is not None:
            env._kill_process(proc)
        env.cleanup()


# Child for the hard-exit race tests: it really os._exit()s right after the kill, like the watchdogs.
_HARD_EXIT_RACE_CHILD = r"""
import json, os, sys, threading, time
from pathlib import Path
import psutil
from tools.environments import base
from tools.environments.local import LocalEnvironment
scenario, cmd, root = sys.argv[1], sys.argv[2], Path(sys.argv[3])
env = LocalEnvironment(cwd=os.getcwd())
spawned, release = threading.Event(), threading.Event()
real_run_bash = LocalEnvironment._run_bash

def gated(self, command, **kw):
    proc = real_run_bash(self, command, **kw)
    if cmd in command:
        # This command execs its sleeper. Only its supervisor and direct workload
        # can survive: publish their identities before the parent permits exit.
        owned = [psutil.Process(pid) for pid in (proc.pid, proc.command_pid)]
        (root / "owned.json").write_text(json.dumps(
            [[p.pid, p.create_time()] for p in owned]))
        (root / "ready").touch()
        spawned.set()
        release.wait(15)
    return proc
LocalEnvironment._run_bash = gated
if scenario == "spawn_before_publish":
    threading.Thread(target=env.execute, args=(cmd,), kwargs={"timeout": 600}, daemon=True).start()
    assert spawned.wait(20)
    while not (root / "observed").exists():
        time.sleep(0.01)
    threading.Timer(0.2, release.set).start()
    base.kill_live_foreground_processes(now=True)
else:  # launch_after_fence
    base.kill_live_foreground_processes(now=True)
    t = threading.Thread(target=env.execute, args=(cmd,), kwargs={"timeout": 600}, daemon=True)
    t.start()
    t.join(3)
    if not spawned.is_set():
        (root / "owned.json").write_text("[]")
        (root / "ready").touch()
    while not (root / "observed").exists():
        time.sleep(0.01)
    release.set()
os._exit(0)
"""


@pytest.mark.platforms("posix")
@pytest.mark.live_system_guard_bypass
@pytest.mark.parametrize("scenario", ["spawn_before_publish", "launch_after_fence"])
def test_hard_exit_leaves_no_foreground_survivor_around_the_spawn(scenario, tmp_path):
    """Hard exit owns both unpublished children and launches racing the exit fence."""
    import sys

    import psutil

    cmd = "exec sleep 30"
    repo = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    owned = []
    controller = subprocess.Popen(
        [sys.executable, "-c", _HARD_EXIT_RACE_CHILD, scenario, cmd, str(tmp_path)],
        cwd=tmp_path, env={**os.environ, "PYTHONPATH": repo},
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        wait_until(lambda: (tmp_path / "ready").exists(), timeout=30,
                   what="synthetic controller published its child identities")
        for pid, created in json.loads((tmp_path / "owned.json").read_text()):
            proc = psutil.Process(pid)
            assert proc.create_time() == created, "test child identity changed before observation"
            owned.append(proc)
        (tmp_path / "observed").touch()
        stdout, stderr = controller.communicate(timeout=30)
        assert controller.returncode == 0, (stdout, stderr)
        wait_until(lambda: all(not p.is_running() or p.status() == psutil.STATUS_ZOMBIE for p in owned),
                   timeout=10, what=f"{scenario} test-owned children exited")
    finally:
        (tmp_path / "observed").touch()
        if controller.poll() is None:
            controller.kill()
        controller.communicate(timeout=10)
        for proc in owned:
            with contextlib.suppress(psutil.Error):
                if proc.is_running() and proc.status() != psutil.STATUS_ZOMBIE:
                    proc.kill()


def test_hard_exit_kill_never_blocks_on_a_slow_remote_cancel(monkeypatch):
    """Modal/Daytona/Vercel cancel through a blocking SDK call; the hard-exit kill runs just before
    os._exit, so it must give those one short deadline instead of waiting them out."""
    from tools.environments import base
    from tools.environments.base_output import _ThreadedProcessHandle

    class _SdkEnv:  # the kill every SDK backend inherits: proc.kill() -> cancel_fn
        _kill_process = base.BaseEnvironment._kill_process
        _force_kill_process = base.BaseEnvironment._force_kill_process

    cancelled = threading.Event()
    handle = _ThreadedProcessHandle(lambda: ("", 0), cancel_fn=lambda: (cancelled.set(), time.sleep(8)))
    monkeypatch.setitem(base._live_foreground, id(handle), (_SdkEnv(), handle))
    t0 = time.monotonic()
    base.kill_live_foreground_processes(now=True)
    elapsed = time.monotonic() - t0
    assert cancelled.is_set() and elapsed < 1.0, f"blocked {elapsed:.2f}s"
