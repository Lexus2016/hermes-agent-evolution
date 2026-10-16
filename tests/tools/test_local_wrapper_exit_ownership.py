"""A supported interruption race can orphan a child before its first observation."""
import shlex
import sys

import psutil
import pytest

from tools.environments import base, local
from tests.waiting import wait_until


def _interrupt_after_wrapper_exit(tmp_path, monkeypatch, mode="interrupt"):
    gate, child_file = tmp_path / "go", tmp_path / "child.pid"
    script = (
        "import os, pathlib, subprocess, sys, time; "
        "gate=pathlib.Path(sys.argv[1]); target=pathlib.Path(sys.argv[2]); "
        "print('ready', flush=True); "
        "\nwhile not gate.exists(): time.sleep(0.001)\n"
        "child=subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'], "
        "stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL); "
        "target.write_text(str(child.pid), encoding='utf-8'); os._exit(0)"
    )
    env = object.__new__(local.LocalEnvironment)
    env.cwd, env.env = str(tmp_path), {}
    proc = env._run_bash("exec " + " ".join(shlex.quote(arg) for arg in
                          [sys.executable, "-c", script, str(gate), str(child_file)]))
    child = None
    try:
        assert proc.stdout.readline().strip() == "ready"
        assert local._owned_process_group(proc) is not None
        assert psutil.Process(getattr(proc, "command_pid", proc.pid)).children(recursive=True) == []

        def interrupted():
            nonlocal child
            # The wait loop has just observed the live wrapper. Its workload is
            # born and orphaned before cleanup gets its first chance to see it.
            gate.touch()
            assert proc.wait(timeout=10) == 0
            child = psutil.Process(int(child_file.read_text(encoding="utf-8")))
            assert child.is_running()
            if mode in {"force", "deadline"}:
                getattr(env, "_force_kill_process" if mode == "force" else "_kill_spawned_tree")(proc)
                wait_until(lambda: not child.is_running() or child.status() == psutil.STATUS_ZOMBIE,
                           timeout=3, what="interrupted test-owned child exited")
            return mode != "timeout"

        monkeypatch.setattr(base, "is_interrupted", interrupted)
        result = env._wait_for_process(proc, timeout=0.001 if mode == "timeout" else 10)
        assert result["returncode"] == (124 if mode == "timeout" else 130)
        wait_until(lambda: not child.is_running() or child.status() == psutil.STATUS_ZOMBIE,
                   timeout=5, what="test-owned child exited after its wrapper")
    finally:
        # Only this test's captured children may be signalled, including on the red run.
        if child is not None and child.is_running():
            child.kill()
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=10)
        proc.stdout.close()
        if hasattr(proc, "release"):
            proc.release()


@pytest.mark.platforms("linux")
@pytest.mark.live_system_guard_bypass
@pytest.mark.parametrize("mode", ["interrupt", "timeout", "force", "deadline"])
def test_interruption_reaps_child_born_before_first_observation_linux(tmp_path, monkeypatch, mode):
    _interrupt_after_wrapper_exit(tmp_path, monkeypatch, mode)


@pytest.mark.platforms("macos")
@pytest.mark.live_system_guard_bypass
@pytest.mark.parametrize("mode", ["interrupt", "timeout", "force", "deadline"])
def test_interruption_reaps_child_born_before_first_observation_macos(tmp_path, monkeypatch, mode):
    _interrupt_after_wrapper_exit(tmp_path, monkeypatch, mode)
