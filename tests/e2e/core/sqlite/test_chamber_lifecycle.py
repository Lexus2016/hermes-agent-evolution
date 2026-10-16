"""Real-child regression for the fd monitor reaping during signal authorization."""

import os
import subprocess
import sys
import threading

import psutil
import pytest

from tests.conformance.persistence._harness import wait_for
from tests.e2e.core.sqlite._helpers import Chamber


@pytest.mark.linux_only
@pytest.mark.parametrize("operation", ["kill9", "sigterm"])
def test_monitor_cannot_reap_during_signal_dispatch(tmp_path, monkeypatch, operation):
    chamber = Chamber(tmp_path)
    chamber._monitor_stop.set()
    chamber._monitor.join(timeout=5)
    attempted = threading.Event()
    finished = threading.Event()
    lock = threading.Lock()

    class ObservedLock:
        def __enter__(self):
            if not lock.acquire(blocking=False):
                attempted.set()
                lock.acquire()
            return self

        def __exit__(self, *exc):
            lock.release()

    chamber._lock = ObservedLock()
    proc = subprocess.Popen(
        [sys.executable, "-c", "import sys; sys.stdin.buffer.read()"],
        stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    proc._stderr_file = open(tmp_path / "worker.stderr", "wb")
    chamber.procs["worker"] = proc
    guarded_kill = os.kill
    original_parents = psutil.Process.parents
    monitors = []

    def scan():
        try:
            chamber.live()  # The fd monitor's real polling/reaping path.
        finally:
            finished.set()
            attempted.set()

    def parents(process):
        if process.pid == proc.pid:
            monitor = threading.Thread(target=scan)
            monitors.append(monitor)
            monitor.start()
            assert attempted.wait(5), "monitor never attempted its lifecycle lock"
        return original_parents(process)

    def exit_before_signal(pid, sig):
        if pid == proc.pid and sig:
            proc.stdin.close()
            wait_for(lambda: psutil.Process(pid).status() == psutil.STATUS_ZOMBIE,
                     deadline=5, what="owned child exit without reaping")
        return guarded_kill(pid, sig)

    monkeypatch.setattr(psutil.Process, "parents", parents)
    monkeypatch.setattr(os, "kill", exit_before_signal)
    try:
        getattr(chamber, operation)("worker")
        assert proc.wait(timeout=5) == 0
        assert finished.wait(5), "monitor did not resume after signal dispatch"
    finally:
        monkeypatch.setattr(psutil.Process, "parents", original_parents)
        monkeypatch.setattr(os, "kill", guarded_kill)
        if not proc.stdin.closed:
            proc.stdin.close()
        proc.wait(timeout=5)
        for monitor in monitors:
            monitor.join(timeout=5)
        chamber.shutdown()
