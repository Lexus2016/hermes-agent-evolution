"""POSIX command supervisor, executed directly in a separate interpreter.

Only this live private process-group leader signals its group. Logical command
exit never authorizes another process to signal a remembered PGID.
"""

import contextlib
import math
import os
import select
import signal
import socket
import subprocess
import sys
import threading
import time


def _snapshot_descendants():
    try:
        import psutil
        return psutil.Process().children(recursive=True)
    except Exception:
        return []


def _bounded_snapshot():
    captured = []
    try:
        worker = threading.Thread(target=lambda: captured.extend(_snapshot_descendants()), daemon=True)
        worker.start()
        worker.join(0.05)
    except Exception:
        return []
    return list(captured)


def _sweep_escaped(descendants, group, force=True):
    for child in descendants:
        with contextlib.suppress(Exception):
            if child.is_running() and os.getpgid(child.pid) != group:
                # psutil rechecks the captured identity before either signal.
                child.kill() if force else child.terminate()


def _bounded_sweep(descendants, group, force):
    with contextlib.suppress(Exception):
        worker = threading.Thread(target=_sweep_escaped, args=(descendants, group, force), daemon=True)
        worker.start()
        worker.join(0.05)


def _owned_group():
    group = os.getpgrp()
    # Never signal the controller's group if startup was changed/intercepted.
    return group if group == os.getpid() and os.getsid(0) == os.getpid() else None


def _cleanup(force, descendants=()):
    """Hard cleanup, also usable before the command/control loop is established."""
    group = _owned_group()
    if group is None:
        return
    captured = [*descendants, *_bounded_snapshot()]
    if not force:
        os.killpg(group, signal.SIGTERM)  # windows-footgun: ok — POSIX helper only
    _bounded_sweep(captured, group, True)
    os.killpg(group, signal.SIGKILL)  # windows-footgun: ok — live owner kills itself last


def _group_empty(group, descendants):
    """Completion evidence only; never acquire PID identities for signalling."""
    import psutil
    for child in descendants:
        if child.is_running() and child.status() != psutil.STATUS_ZOMBIE:
            return False
    for process in psutil.process_iter():
        # Darwin exposes kernel_task as PID 0. getpgid(0) means *our* group,
        # not that kernel entry, and would falsely keep every group alive.
        if process.pid <= 0 or process.pid == os.getpid():
            continue
        try:
            if os.getpgid(process.pid) == group and process.status() != psutil.STATUS_ZOMBIE:
                return False
        except (ProcessLookupError, psutil.NoSuchProcess):
            pass
        # Access failures and unknown state are deliberately not empty evidence.
    return True


def _poll_empty(probe, group, descendants):
    """At most one optional enumeration worker; a stalled probe retains ownership."""
    if probe is not None:
        worker, result = probe
        if worker.is_alive():
            return probe, False
        if result == [True]:
            return None, True
    result = []
    def check():
        with contextlib.suppress(Exception):
            result.append(_group_empty(group, descendants))
    try:
        worker = threading.Thread(target=check, daemon=True)
        worker.start()
        return (worker, result), False
    except Exception:
        return None, False


def _close_stdio():
    null = os.open(os.devnull, os.O_RDWR)
    try:
        for fd in (0, 1, 2):
            os.dup2(null, fd)
    finally:
        if null > 2:
            os.close(null)


def main():
    control = socket.socket(fileno=int(sys.argv[1]))
    control.settimeout(5)
    # Darwin gateway spawn shims have historically dropped start_new_session.
    os.setsid()  # windows-footgun: ok — launched only on POSIX
    group = _owned_group()
    if group is None:
        raise RuntimeError("private process group unavailable")
    # A caught handler resets to default on exec; SIG_IGN would leak to bash.
    signal.signal(signal.SIGTERM, lambda *_: None)
    signal.signal(signal.SIGINT, lambda *_: None)
    control.sendall(b"READY\n")
    if control.recv(1) != b"S":
        return 1
    proc = subprocess.Popen(sys.argv[2:], stdin=0, stdout=1, stderr=2, close_fds=True)
    control.sendall(f"STARTED {proc.pid}\n".encode())
    _close_stdio()  # only the workload holds inherited stdin/stdout pipes
    control.setblocking(False)
    reported = release = False
    commands = b""
    phase, deadline, grace = None, 0.0, 0.0
    descendants, probe = [], None
    while True:
        code = proc.poll()  # direct child stays unreused until reaped here
        if code is not None and not reported:
            control.sendall(f"EXIT {code}\n".encode())
            reported = True
        if release and reported and phase is None:
            return 0  # intentional background jobs keep their existing lifetime
        ready, _, _ = select.select([control], [], [], 0.02)
        if ready:
            try:
                chunk = control.recv(256)
            except BlockingIOError:
                chunk = None
            if chunk == b"":
                _cleanup(True, descendants)
                return 1
            if chunk:
                commands += chunk
            if len(commands) > 1024:
                raise RuntimeError("invalid owner control frame")
            while b"\n" in commands:
                command, commands = commands.split(b"\n", 1)
                if command == b"K":
                    _cleanup(True, descendants)
                    return 1
                if command == b"T":
                    descendants.extend(_bounded_snapshot())
                    os.killpg(group, signal.SIGTERM)  # windows-footgun: ok — live POSIX owner
                    phase, deadline = "foreground", time.monotonic() + 0.5
                elif command.startswith(b"G "):
                    grace = float(command[2:])
                    if not math.isfinite(grace) or grace < 0:
                        raise RuntimeError("invalid owner grace")
                    descendants.extend(_bounded_snapshot())
                    # Popen never signals a reaped PID: the direct child is the
                    # logical parent, not this helper or its whole group.
                    if proc.poll() is None:
                        proc.terminate()
                    phase, deadline, probe = "parent", time.monotonic() + grace, None
                elif command == b"R" and phase is None:
                    release = True
        now = time.monotonic()
        if phase == "foreground" and (proc.poll() is not None or now >= deadline):
            _cleanup(True, descendants)
            return 1
        if phase == "parent" and (proc.poll() is not None or now >= deadline):
            # Preserve self-managing children throughout the first parent-only
            # window. Only its survivors receive the second TERM phase.
            os.killpg(group, signal.SIGTERM)  # windows-footgun: ok — live POSIX owner
            _bounded_sweep(descendants, group, False)
            phase, deadline = "descendants", time.monotonic() + grace
            if grace == 0:
                control.sendall(b"INCOMPLETE\n")
        if phase == "descendants":
            probe, empty = _poll_empty(probe, group, descendants)
            if empty:
                # The empty probe can observe a new zombie between the loop's
                # poll and this point. Reap/report it before ending ownership.
                if not reported:
                    code = proc.poll()
                    if code is None:
                        continue
                    control.sendall(f"EXIT {code}\n".encode())
                control.sendall(b"CLEANED\n")
                return 0
            if grace > 0 and time.monotonic() >= deadline:
                _cleanup(True, descendants)
                return 1
            # grace=0 disables escalation. Keep serving force/EOF, a later G,
            # and empty-group checks; never silently release a surviving tree.


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:
        _cleanup(True)
        raise
