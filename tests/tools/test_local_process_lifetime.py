"""Native contracts for the private foreground process-group lifetime owner."""

import contextlib
import os
from pathlib import Path
import shlex
import signal
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import psutil
import pytest

from tools.environments import base, local
from tests.waiting import wait_until


def _spawn(tmp_path, code, *args, stdin_data=None):
    env = object.__new__(local.LocalEnvironment)
    env.cwd, env.env = str(tmp_path), {}
    command = "exec " + " ".join(map(shlex.quote, [sys.executable, "-c", code, *map(str, args)]))
    return env, env._run_bash(command, stdin_data=stdin_data)


def _alive(child):
    return child.is_running() and child.status() != psutil.STATUS_ZOMBIE


def _gone(child):
    wait_until(lambda: not _alive(child), timeout=5,
               what=f"test-owned process {child.pid} exited")


def _finish(proc):
    if proc._owner.poll() is None:
        proc.terminate_owned(force=True)
    proc.release()
    for stream in (proc.stdin, proc.stdout):
        if stream is not None:
            with contextlib.suppress(OSError, ValueError):
                stream.close()


def _contract(tmp_path, monkeypatch, caplog, case):
    child = None
    proc = None
    try:
        if case == "io_status":
            payload = "alpha\nβeta\n" * 20000
            env, proc = _spawn(tmp_path, "import sys; print(sys.stdin.read(), end=''); sys.exit(7)", stdin_data=payload)
            result = env._wait_for_process(proc, timeout=15)
            assert result == {"output": payload, "returncode": 7}
            assert proc._owner.wait(timeout=3) == 0
        elif case == "negative_signal":
            env, proc = _spawn(tmp_path, "import os,signal; os.kill(os.getpid(), signal.SIGTERM)")
            assert env._wait_for_process(proc, timeout=10)["returncode"] == -signal.SIGTERM
        elif case == "early_stdin_close":
            env, proc = _spawn(tmp_path, "import os,time; os.close(0); print('done',flush=True); time.sleep(.1)",
                               stdin_data="x" * 1000000)
            result = env._wait_for_process(proc, timeout=10)
            assert result == {"output": "done\n", "returncode": 0}
        elif case == "early_stdout_close":
            env, proc = _spawn(tmp_path, "import os,time; os.close(1); os.close(2); time.sleep(30)")
            assert proc.stdout.read() == ""
            assert proc.poll() is None
            proc.release()  # a premature release must retain the running workload
            assert proc._owner.poll() is None
            result = env._wait_for_process(proc, timeout=0.2)
            assert result["returncode"] == 124
        elif case in {"normal_background", "helper_lost"}:
            file = tmp_path / "pid"
            code = ("import os,pathlib,subprocess,sys; "
                    "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)'], "
                    "stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL); "
                    "pathlib.Path(sys.argv[1]).write_text(str(p.pid)); print('ready',flush=True); ")
            code += "os._exit(0)" if case == "normal_background" else "import time; time.sleep(30)"
            env, proc = _spawn(tmp_path, code, file)
            assert proc.stdout.readline().strip() == "ready"
            child = psutil.Process(int(file.read_text()))
            if case == "normal_background":
                assert env._wait_for_process(proc, timeout=10)["returncode"] == 0
                assert proc._owner.wait(timeout=3) == 0
                assert _alive(child), "normal completion killed an intentionally backgrounded command"
            else:
                workload = psutil.Process(proc.command_pid)
                proc._owner.kill()  # only this test's own helper; simulate external loss
                proc._owner.wait(timeout=3)
                monkeypatch.setattr(local.os, "killpg", lambda *a: pytest.fail("adopted lost owner's PGID"))
                env._kill_process(proc)
                assert "cleanup incomplete" in caplog.text
                assert _alive(child)
                from tools.process_registry import ProcessRegistry
                registry = ProcessRegistry()
                session = registry._new_session("owned test workload", "", "", "", str(tmp_path))
                session.process, session.pid = proc, proc.pid
                monkeypatch.setattr(registry, "_terminate_host_pid", lambda *a: pytest.fail("raw PID fallback"))
                result = registry._signal_kill(session, session.id, False)
                assert result["status"] == "error" and result["process_running"]
                workload.kill()
        elif case in {"failed_yield", "yield_complete", "yield_kill"}:
            gate = tmp_path / "finish"
            code = ("import pathlib,sys,time; print('ready',flush=True); "
                    "gate=pathlib.Path(sys.argv[1]); "
                    "\nwhile not gate.exists(): time.sleep(.01)\nprint('finished',flush=True)")
            env, proc = _spawn(tmp_path, code, gate)
            assert proc.stdout.readline().strip() == "ready"
            requested = [True]
            monkeypatch.setattr(base, "consume_yield", lambda *_: requested.pop() if requested else False)
            if case == "failed_yield":
                from tools.process_registry import ProcessRegistry
                registry = ProcessRegistry()
                def unavailable(*_):
                    raise RuntimeError("test registry reader could not start")
                monkeypatch.setattr(registry, "_track_started", unavailable)
                def handoff(handle, output):
                    gate.touch()
                    return registry.adopt_local(handle, command="owned test workload", cwd=str(tmp_path))
                result = env._wait_for_process(proc, timeout=10, yield_handler=handoff)
                assert result["returncode"] == 0 and "finished" in result["output"]
                assert proc._owner.wait(timeout=3) == 0
            else:
                from tools.process_registry import ProcessRegistry
                registry = ProcessRegistry()
                sessions = []
                def handoff(handle, output):
                    session = registry.adopt_local(handle, command="owned test workload", cwd=str(tmp_path),
                                                   output_so_far=output, notify_on_complete=False)
                    sessions.append(session)
                    return {"yielded_session_id": session.id, "returncode": None}
                result = env._wait_for_process(proc, timeout=10, yield_handler=handoff)
                assert result["yielded_session_id"] == sessions[0].id
                assert proc.poll() is None and proc._owner.poll() is None
                if case == "yield_kill":
                    assert registry.kill_process(sessions[0].id)["status"] == "killed"
                else:
                    gate.touch()
                    wait_until(lambda: sessions[0].exited, timeout=10,
                               what="adopted command completed")
                    assert sessions[0].exit_code == 0
                assert proc._owner.wait(timeout=5) is not None
        elif case in {"release_wins", "cancel_wins"}:
            file = tmp_path / "pid"
            code = ("import os,pathlib,subprocess,sys; "
                    "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)'], "
                    "stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL); "
                    "pathlib.Path(sys.argv[1]).write_text(str(p.pid)); os._exit(0)")
            env, proc = _spawn(tmp_path, code, file)
            assert proc.wait(timeout=10) == 0
            child = psutil.Process(int(file.read_text()))
            entered, unblock, competing = threading.Event(), threading.Event(), threading.Event()
            send = proc._send
            def paused(command):
                entered.set()
                assert unblock.wait(5)
                return send(command)
            monkeypatch.setattr(proc, "_send", paused)
            cancel = lambda: proc.terminate_owned(force=True)
            first, second = (proc.release, cancel) if case == "release_wins" else (cancel, proc.release)
            winner = threading.Thread(target=first)
            loser = threading.Thread(target=lambda: (competing.set(), second()))
            winner.start()
            assert entered.wait(5)
            loser.start()
            assert competing.wait(5)
            unblock.set()
            winner.join(5)
            loser.join(5)
            assert not winner.is_alive() and not loser.is_alive()
            if case == "release_wins":
                assert proc._state == "released" and _alive(child)
            else:
                assert proc._state == "cancelled"
                _gone(child)
        elif case in {"stalled_discovery", "unavailable_threads"}:
            file = tmp_path / "pid"
            repo = Path(__file__).resolve().parents[2]
            sabotage = ("owner._snapshot_descendants=lambda: time.sleep(30); " if case == "stalled_discovery" else
                        "owner.threading.Thread.start=lambda self: (_ for _ in ()).throw(RuntimeError('unavailable')); ")
            code = ("import os,pathlib,subprocess,sys,time; "
                    "from tools.environments import local_process_owner as owner; os.setsid(); "
                    "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)']); "
                    "pathlib.Path(sys.argv[1]).write_text(str(p.pid)); "
                    + sabotage + "owner._cleanup(True)")
            result = subprocess.run([sys.executable, "-c", code, str(file)],
                                    env={**os.environ, "PYTHONPATH": str(repo)}, capture_output=True, timeout=5, check=False)
            assert result.returncode == -signal.SIGKILL
            try:
                child = psutil.Process(int(file.read_text()))
            except psutil.NoSuchProcess:
                return
            _gone(child)
        elif case in {"empty_zero_pid", "empty_live_member", "empty_inaccessible"}:
            from tools.environments import local_process_owner as owner
            member = SimpleNamespace(pid=0 if case == "empty_zero_pid" else 424242,
                                     status=lambda: psutil.STATUS_RUNNING)
            monkeypatch.setattr(psutil, "process_iter", lambda: [member])
            def group(pid):
                if case == "empty_inaccessible":
                    raise PermissionError("membership unknown")
                return 31337
            monkeypatch.setattr(owner.os, "getpgid", group)
            if case == "empty_inaccessible":
                with pytest.raises(PermissionError):
                    owner._group_empty(31337, [])
            else:
                assert owner._group_empty(31337, []) is (case == "empty_zero_pid")
        elif case.startswith("grace_"):
            file = tmp_path / "terms"
            code = ("import pathlib,signal,sys,time; p=pathlib.Path(sys.argv[1]); "
                    "signal.signal(signal.SIGTERM,lambda *a: p.open('a').write('term\\n')); "
                    "print('ready',flush=True); time.sleep(30)")
            env, proc = _spawn(tmp_path, code, file)
            child = psutil.Process(proc.command_pid)
            assert proc.stdout.readline().strip() == "ready"
            assert proc.terminate_owned(term_grace=2, wait=False)
            target = 2 if "descendants" in case else 1
            wait_until(lambda: file.exists() and len(file.read_text().splitlines()) >= target,
                       timeout=5, what="configured grace phase started")
            started = time.monotonic()
            if case.endswith("force"):
                assert proc.terminate_owned(force=True)
            else:
                proc._close_control()  # real private-channel EOF during this phase
                proc._owner.wait(timeout=2)
            assert time.monotonic() - started < 2, "force/EOF waited for configured TERM grace"
            _gone(child)
        elif case in {"empty_evidence_stalled", "empty_evidence_unavailable"}:
            from tools.environments import local_process
            popen = subprocess.Popen
            repo = Path(__file__).resolve().parents[2]
            probe_started = tmp_path / "empty-probe-started"
            failure = ("time.sleep(30)" if case == "empty_evidence_stalled" else
                       "(_ for _ in ()).throw(OSError('unavailable'))")
            wrapper = (f"import sys,time,pathlib; sys.path.insert(0,{str(repo)!r}); "
                       "from tools.environments import local_process_owner as owner; "
                       f"owner._group_empty=lambda *a: (pathlib.Path({str(probe_started)!r}).touch(), {failure})[-1]; "
                       "sys.exit(owner.main())")
            def sabotaged(args, **kwargs):
                return popen([args[0], "-c", wrapper, *args[3:]], **kwargs)
            monkeypatch.setattr(local_process.subprocess, "Popen", sabotaged)
            env, proc = _spawn(tmp_path, "import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); "
                               "print('ready',flush=True); time.sleep(30)")
            child = psutil.Process(proc.command_pid)
            assert proc.stdout.readline().strip() == "ready"
            assert not proc.terminate_owned(term_grace=0)
            wait_until(probe_started.exists, timeout=5, what="empty-group evidence probe started")
            assert proc.poll() is None and proc._owner.poll() is None
            assert _alive(child), "missing empty-group evidence caused escalation"
            started = time.monotonic()
            assert proc.terminate_owned(force=True)
            assert time.monotonic() - started < 2
            _gone(child)
        elif case == "startup_refusal":
            file = tmp_path / "must-not-run"
            from tools.environments import local_process
            monkeypatch.setattr(local_process.os, "getpgid", lambda pid: os.getpgrp())
            with pytest.raises(RuntimeError, match="private process group"):
                _spawn(tmp_path, "import pathlib,sys; pathlib.Path(sys.argv[1]).touch()", file)
            assert not file.exists(), "workload dispatched before private ownership was verified"
        elif case == "parent_eof":
            # No kill funnel: abrupt controller death must close its private
            # socket. A command inheriting that FD would prevent this cleanup.
            file = tmp_path / "child"
            repo = Path(__file__).resolve().parents[2]
            code = ("import os,sys; from tools.environments import local; "
                    "e=object.__new__(local.LocalEnvironment); e.cwd=sys.argv[1]; e.env={}; "
                    "p=e._run_bash(sys.argv[2]); "
                    "assert p.stdout.readline().strip()=='ready'; os._exit(0)")
            workload = ("import os,pathlib,sys,time; pathlib.Path(sys.argv[1]).write_text(str(os.getpid())); "
                        "print('ready',flush=True); time.sleep(30)")
            cmd = "exec " + " ".join(map(shlex.quote, [sys.executable, "-c", workload, str(file)]))
            result = subprocess.run([sys.executable, "-c", code, str(tmp_path), cmd],
                                    env={**os.environ, "PYTHONPATH": str(repo)}, capture_output=True, timeout=15, check=False)
            assert result.returncode == 0, result.stderr
            try:
                child = psutil.Process(int(file.read_text()))
            except psutil.NoSuchProcess:
                return
            _gone(child)
    finally:
        if child is not None and _alive(child):
            child.kill()
        if proc is not None:
            _finish(proc)


_CASES = ["io_status", "negative_signal", "early_stdin_close", "early_stdout_close", "normal_background",
          "helper_lost", "failed_yield", "yield_complete", "yield_kill", "parent_eof",
          "release_wins", "cancel_wins", "stalled_discovery", "unavailable_threads",
          "empty_evidence_stalled", "empty_evidence_unavailable", "startup_refusal",
          "empty_zero_pid", "empty_live_member", "empty_inaccessible",
          "grace_parent_force", "grace_parent_eof", "grace_descendants_force", "grace_descendants_eof"]


@pytest.mark.platforms("linux")
@pytest.mark.live_system_guard_bypass
@pytest.mark.parametrize("case", _CASES)
def test_owned_lifetime_contract_linux(tmp_path, monkeypatch, caplog, case):
    _contract(tmp_path, monkeypatch, caplog, case)


@pytest.mark.platforms("macos")
@pytest.mark.live_system_guard_bypass
@pytest.mark.parametrize("case", _CASES)
def test_owned_lifetime_contract_macos(tmp_path, monkeypatch, caplog, case):
    _contract(tmp_path, monkeypatch, caplog, case)


@pytest.mark.platforms("windows")
@pytest.mark.live_system_guard_bypass
def test_windows_retains_native_popen_stdio_and_status(tmp_path):
    env = object.__new__(local.LocalEnvironment)
    env.cwd, env.env = str(tmp_path), {}
    proc = env._run_bash("read line; printf '%s\\n' \"$line\"; exit 7", stdin_data="owned input\n")
    try:
        assert isinstance(proc, subprocess.Popen)
        assert env._wait_for_process(proc, timeout=15) == {"output": "owned input\n", "returncode": 7}
    finally:
        if proc.poll() is None:
            env._kill_process(proc)
        proc.wait(timeout=10)
        proc.stdout.close()
