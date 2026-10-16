"""Isolated 0.6.1 API launcher. Run only with the PM-selected interpreter."""
from __future__ import annotations

import importlib
import importlib.metadata
import json
import os
import secrets
import signal
import socket
import sys
import threading


def _verify_runtime() -> None:
    if sys.version_info[:2] != (3, 12):
        raise RuntimeError("Embedded Hindsight requires its isolated Python 3.12 runtime")
    for distribution in ("hindsight-all", "hindsight-embed", "hindsight-client", "hindsight-api-slim"):
        if importlib.metadata.version(distribution) != "0.6.1":
            raise RuntimeError(f"Embedded runtime requires {distribution}==0.6.1; rerun hermes memory setup")
    for distribution, version in (("pg0-embedded", "0.15.2"), ("psutil", "7.2.2")):
        if importlib.metadata.version(distribution) != version:
            raise RuntimeError(f"Embedded runtime requires {distribution}=={version}")
    importlib.import_module("hindsight_api.main")
    importlib.import_module("sentence_transformers")
    pg0 = importlib.import_module("pg0")
    binary = pg0._get_bundled_binary()
    if binary is None:
        raise RuntimeError("The pinned pg0 wheel has no bundled executable for this platform")
    pg0._find_pg0 = lambda: str(binary)


def _verify_owned_process(process, nonce: str, created: float) -> None:
    """Validate a private inherited launch token and stable OS process identity."""
    import psutil

    if (process.create_time() != created
            or process.environ().get("HERMES_HINDSIGHT_OWNER_NONCE") != nonce
            or psutil.Process(process.pid).create_time() != created):
        raise RuntimeError("PostgreSQL process does not belong to this launcher generation")


def _terminate_owned_process(process, nonce: str, created: float) -> None:
    import psutil

    if process.is_running() and process.status() != psutil.STATUS_ZOMBIE:
        _verify_owned_process(process, nonce, created)
        # psutil rechecks captured PID/create-time identity when delivering signals.
        process.terminate()


def main() -> None:
    if sys.argv[1:] == ["--probe"]:
        _verify_runtime()
        return
    # Duplicate the readiness channel before upstream daemonize redirects stdio.
    readiness = os.fdopen(os.dup(sys.stdout.fileno()), "w", encoding="utf-8", buffering=1)  # windows-footgun: ok — write-only pipe, no BOM emitted
    parent_pipe = os.fdopen(os.dup(sys.stdin.fileno()), "r", encoding="utf-8-sig")
    settings = json.loads(parent_pipe.readline())
    os.dup2(sys.stderr.fileno(), sys.stdout.fileno())
    sys.stdout = sys.stderr
    for key, value in settings["environment"].items():
        os.environ[key] = value
    os.environ["_HINDSIGHT_DAEMON_CHILD"] = "1"
    _verify_runtime()
    import hindsight_api.main as api
    import uvicorn

    # Hold the socket throughout startup: no bind-close-rebind window and no adoption.
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(128)
    port = listener.getsockname()[1]

    # pg0's command can daemonize PostgreSQL before its own process exits. A private
    # start receipt, including process creation time, is the ownership boundary.
    import psutil
    from pg0 import Pg0
    from hindsight_api.pg0 import EmbeddedPostgres

    owned = {}
    spawn_inflight = False
    send_lock = threading.Lock()
    generation = settings["generation"]
    ownership_nonce = secrets.token_urlsafe(32)
    # health: allow HX002 -- isolated child ownership nonce only; never host config/argv/ledger; process identity and no-host-mutation tests cover this boundary
    os.environ["HERMES_HINDSIGHT_OWNER_NONCE"] = ownership_nonce

    def send(event, **values):
        with send_lock:
            readiness.write(json.dumps(dict(event=event, generation=generation, **values)) + "\n")

    original_start = Pg0.start
    original_running = EmbeddedPostgres.is_running

    async def refuse_existing(instance):
        if await original_running(instance):
            raise RuntimeError("Embedded database is already running outside this owned launcher; stop it before retrying")
        return False

    def start_owned(instance):
        nonlocal spawn_inflight
        spawn_inflight = True
        info = original_start(instance)
        if not info.pid:
            raise RuntimeError("pg0 did not provide a process ownership receipt")
        process = psutil.Process(info.pid)
        created = process.create_time()
        _verify_owned_process(process, ownership_nonce, created)
        owned[instance.name] = (instance, process, created)
        send("owned", pid=process.pid, created=created)
        spawn_inflight = False
        return info

    def stop_owned(instance):
        record = owned.get(instance.name)
        if record is None:
            raise RuntimeError("Refusing to stop a PostgreSQL instance not started by this launcher")
        _terminate_owned_process(record[1], ownership_nonce, record[2])

    Pg0.start, Pg0.stop = start_owned, stop_owned
    EmbeddedPostgres.is_running = refuse_existing

    def run_reserved(**kwargs):
        kwargs.update(host="127.0.0.1", port=port, workers=1, reload=False)
        server = uvicorn.Server(uvicorn.Config(**kwargs))

        def report_ready():
            while not server.started and not server.should_exit:
                threading.Event().wait(0.05)
            if server.started:
                send("ready", url=f"http://127.0.0.1:{port}")

        def parent_closed():
            parent_pipe.read()
            os.kill(os.getpid(), signal.SIGTERM)

        threading.Thread(target=parent_closed, daemon=True).start()
        threading.Thread(target=report_ready, daemon=True).start()
        server.run(sockets=[listener])

    # This pinned CLI has one uvicorn.run call; preserve upstream config, idle middleware,
    # migrations and PostgreSQL cleanup while supplying the already-owned socket.
    uvicorn.run = run_reserved
    sys.argv = ["hindsight-api", "--host", "127.0.0.1", "--port", str(port),
                "--idle-timeout", str(settings["idle_timeout"])]
    try:
        api.main()
    finally:
        listener.close()
        try:
            for instance, process, _created in owned.values():
                stop_owned(instance)
                if process.is_running() and process.status() != psutil.STATUS_ZOMBIE:
                    try:
                        process.wait(timeout=10)
                    except psutil.TimeoutExpired:
                        if process.is_running() and process.status() != psutil.STATUS_ZOMBIE:
                            raise RuntimeError("Owned PostgreSQL process did not finish cleanup") from None
            if spawn_inflight:
                raise RuntimeError("PostgreSQL startup ownership receipt was incomplete")
            send("clean")
        finally:
            readiness.close()
            parent_pipe.close()


if __name__ == "__main__":
    main()
