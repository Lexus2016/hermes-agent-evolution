"""Owned subprocess lifecycle and async client leases for embedded Hindsight."""
from __future__ import annotations

import asyncio
import contextlib
import json
import math
import os
import queue
import secrets
import subprocess
import threading
import time
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

from hermes_constants import get_hermes_home
from tools.environments.local import hermes_subprocess_env

from .isolated_runtime import database_identity, runtime_cache_key, runtime_python


class OwnedDaemon:
    def __init__(self, home: Path, identity: str, environment: dict, idle_timeout: int,
                 health_timeout: float = 120, health_grace: float = 30, python: Path | None = None):
        self.home, self.identity = home, identity
        self.python = python
        self.os_home = Path.home()
        self._descendants = {}
        self.environment = dict(environment)
        self.idle_timeout, self.health_timeout = idle_timeout, health_timeout
        self.health_grace = health_grace
        self.api_key = secrets.token_urlsafe(32)
        self.process = None
        self.url = None
        self._lock = threading.RLock()
        self._file_lock = None
        self.references = 0
        self._events = queue.Queue()
        self._reader = None
        self._clean_receipt = False
        self._uncertain = False
        self.generation = secrets.token_hex(16)
        self._ledger = self.home / "hindsight" / f"{self.identity}.lifecycle.json"

    def _acquire_lock(self):
        # File locking prevents two independent Hermes processes writing the same DB.
        from gateway.status import _try_acquire_file_lock

        if self._ledger.exists():
            raise RuntimeError(self._recovery_message())
        path = self.home / "hindsight" / f"{self.identity}.lock"
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = path.open("a+", encoding="utf-8")
        if not _try_acquire_file_lock(handle):
            handle.close()
            raise RuntimeError("Embedded Hindsight database is owned by another Hermes process; stop that process first")
        self._file_lock = handle
        self._write_ledger("starting")

    def _release_lock(self):
        if self._file_lock is not None:
            from gateway.status import _release_file_lock

            _release_file_lock(self._file_lock)
            self._file_lock.close()
            self._file_lock = None

    def _command(self):
        return [str(self.python or runtime_python()), "-I", str(Path(__file__).with_name("daemon_launcher.py"))]

    def _launch(self):
        log = self.home / "logs" / "hindsight-embed.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        env = hermes_subprocess_env()
        # No launch-profile Hindsight configuration, Python injection, or proxy routing.
        env = {k: v for k, v in env.items() if not k.upper().startswith(("HINDSIGHT_", "PYTHON"))
               and k.upper() not in {"HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"}}
        env["USERPROFILE" if os.name == "nt" else "HOME"] = str(self.os_home)
        settings = dict(self.environment,
                        HINDSIGHT_API_DATABASE_URL=f"pg0://{self.identity}",
                        HINDSIGHT_API_DAEMON_LOG=str(log),
                        HINDSIGHT_API_TENANT_EXTENSION="hindsight_api.extensions.builtin.tenant:ApiKeyTenantExtension",
                        HINDSIGHT_API_TENANT_API_KEY=self.api_key,
                        HINDSIGHT_API_MCP_ENABLED="false")
        command = self._command()
        with log.open("ab") as output:
            self.process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                            stderr=output, env=env, text=True, encoding="utf-8",
                                            start_new_session=(os.name == "posix"))
        self.process.stdin.write(json.dumps({"environment": settings, "idle_timeout": self.idle_timeout,
                                            "generation": self.generation}) + "\n")
        self.process.stdin.flush()
        process = self.process

        def read_events():
            for line in process.stdout:
                self._events.put(line)
            self._events.put(None)

        self._reader = threading.Thread(target=read_events, daemon=True)
        self._reader.start()
        deadline = time.monotonic() + self.health_timeout
        while time.monotonic() < deadline:
            try:
                line = self._events.get(timeout=max(0.001, deadline - time.monotonic()))
            except queue.Empty as exc:
                raise RuntimeError("Embedded Hindsight readiness timed out; inspect hindsight-embed.log") from exc
            if line is None:
                raise RuntimeError("Embedded Hindsight exited before readiness; inspect hindsight-embed.log")
            event = self._consume_event(line)
            if event == "ready":
                return
        raise RuntimeError("Embedded Hindsight readiness timed out")

    def _consume_event(self, line):
        import psutil

        data = json.loads(line)
        if data.get("generation") != self.generation:
            raise RuntimeError("Embedded Hindsight lifecycle generation mismatch")
        event = data.get("event")
        if event == "owned":
            try:
                process = psutil.Process(int(data["pid"]))
                created = process.create_time()
            except psutil.NoSuchProcess:
                return event
            if created != float(data["created"]):
                raise RuntimeError("Embedded PostgreSQL process identity changed before its ownership receipt")
            self._descendants[(process.pid, created)] = process
            self._write_ledger("owned")
        elif event == "ready":
            url = data.get("url", "")
            parsed = urlsplit(url)
            if parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or not parsed.port or parsed.path:
                raise RuntimeError("Embedded Hindsight returned an invalid loopback endpoint")
            self.url = url
        elif event == "clean":
            self._clean_receipt = True
        else:
            raise RuntimeError("Unknown embedded Hindsight lifecycle receipt")
        return event

    def _drain_events(self):
        while True:
            try:
                line = self._events.get_nowait()
            except queue.Empty:
                return
            if line is not None:
                self._consume_event(line)

    def _write_ledger(self, state):
        self._ledger.parent.mkdir(parents=True, exist_ok=True)
        content = {"generation": self.generation, "state": state,
                   "processes": [{"pid": pid, "created": created} for pid, created in self._descendants]}
        self._ledger.write_text(json.dumps(content), encoding="utf-8")

    def _recovery_message(self):
        return (f"Embedded Hindsight cleanup could not be proven; restart is blocked by {self._ledger}. "
                "An operator must verify the recorded process identities and database are stopped, preserve/back up "
                "the database, and only then clear this lifecycle record. Never kill a process by PID/port alone.")

    def _capture_descendants(self):
        import psutil

        if self.process is None or self.process.poll() is not None:
            return
        try:
            children = psutil.Process(self.process.pid).children(recursive=True)
            for child in children:
                self._descendants[(child.pid, child.create_time())] = child
        except psutil.NoSuchProcess:
            return

    def _healthy(self) -> bool:
        self._capture_descendants()
        if self.process is None or self.process.poll() is not None or self.url is None:
            return False
        request = urllib.request.Request(self.url + "/health")
        try:
            # Never send a local API token through a configured HTTP proxy.
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(request, timeout=min(self.health_grace, self.health_timeout)) as response:
                healthy = response.status == 200
                self._capture_descendants()
                return healthy
        except OSError:
            return False

    def ensure_started(self) -> str:
        with self._lock:
            if self._uncertain:
                raise RuntimeError(self._recovery_message())
            self._drain_events()
            if self._healthy():
                return self.url
            self.stop()
            self.generation = secrets.token_hex(16)
            self._acquire_lock()
            self._clean_receipt = False
            self._events = queue.Queue()
            try:
                self._launch()
                deadline = time.monotonic() + self.health_timeout
                while self.process.poll() is None and time.monotonic() < deadline:
                    if self._healthy():
                        return self.url
                    threading.Event().wait(0.1)
                raise RuntimeError("Embedded Hindsight failed its health deadline; inspect hindsight-embed.log")
            except BaseException:
                self.stop()
                raise

    def stop(self):
        with self._lock:
            self._capture_descendants()
            process, self.process = self.process, None
            self.url = None
            if process is None:
                if self._uncertain:
                    raise RuntimeError(self._recovery_message())
                if self._file_lock is not None:
                    self._ledger.unlink(missing_ok=True)
                self._release_lock()
                return
            try:
                _stop_owned_process(process, list(self._descendants.values()))
                if self._reader is not None:
                    self._reader.join(timeout=2)
                self._drain_events()
                # Reap descendants received late, including detached PostgreSQL from
                # the trusted launcher's pg0 start result rather than a PID scan.
                _stop_owned_process(process, list(self._descendants.values()))
                if not self._clean_receipt or _live_descendants(self._descendants.values()):
                    raise RuntimeError(self._recovery_message())
            except BaseException:
                self._uncertain = True
                self._write_ledger("unresolved")
                raise
            finally:
                for stream in (process.stdin, process.stdout):
                    if stream and not (stream is process.stdout and self._reader is not None and self._reader.is_alive()):
                        stream.close()
            self._ledger.unlink(missing_ok=True)
            self._descendants.clear()
            self._release_lock()


_cache = {}
_cache_lock = threading.Lock()


class ManagedEmbeddedClient:
    """One async HTTP client per provider; shared owned daemon per scoped configuration."""
    def __init__(self, *, profile, llm_provider, llm_api_key, llm_model, idle_timeout,
                 llm_base_url="", config=None):
        home = get_hermes_home()
        config = dict(config or {}, profile=profile)
        identity = database_identity(config, home=home)
        env = {"HINDSIGHT_API_LLM_PROVIDER": llm_provider, "HINDSIGHT_API_LLM_API_KEY": llm_api_key,
               "HINDSIGHT_API_LLM_MODEL": llm_model, "HINDSIGHT_API_LOG_LEVEL": "info"}
        if llm_base_url:
            env["HINDSIGHT_API_LLM_BASE_URL"] = llm_base_url
        python = runtime_python()
        self._key = runtime_cache_key(home, identity, {**env, "idle_timeout": idle_timeout,
                                                    "interpreter": str(python),
                                                    "health_grace": config.get("port_health_grace_timeout"),
                                                    "startup_timeout": config.get("startup_timeout")})
        with _cache_lock:
            runtime = _cache.get(self._key)
            if runtime is None:
                runtime = OwnedDaemon(home, identity, env, idle_timeout,
                                      health_timeout=_positive_timeout(config.get("startup_timeout"), 120),
                                      health_grace=_positive_timeout(config.get("port_health_grace_timeout"), 30), python=python)
                _cache[self._key] = runtime
            runtime.references += 1
        self._runtime = runtime
        self._client = None
        self._client_url = None
        self._closed = False
        self._async_lock = asyncio.Lock()

    @property
    def url(self):
        return self._runtime.url

    def _ensure_started(self):
        if self._closed:
            raise RuntimeError("Embedded Hindsight client is closed")
        return self._runtime.ensure_started()

    async def _prepare(self):
        from hindsight_client import Hindsight

        async with self._async_lock:
            url = await asyncio.to_thread(self._ensure_started)
            if self._client is None or self._client_url != url:
                if self._client is not None:
                    await self._client.aclose()
                self._client = Hindsight(base_url=url, api_key=self._runtime.api_key)
                self._client_url = url
            return self._client

    @property
    def operations(self):
        return _Operations(self)

    def __getattr__(self, name):
        if not name.startswith("a"):
            raise AttributeError(name)

        async def call(*args, **kwargs):
            client = await self._prepare()
            return await getattr(client, name)(*args, **kwargs)
        return call

    async def aclose(self):
        if self._closed:
            return
        self._closed = True
        try:
            if self._client is not None:
                await self._client.aclose()
                self._client = None
        finally:
            await asyncio.to_thread(_release_runtime, self._key, self._runtime)



class _Operations:
    def __init__(self, owner):
        self.owner = owner

    async def get_operation_status(self, **kwargs):
        client = await self.owner._prepare()
        return await client.operations.get_operation_status(**kwargs)


def _positive_timeout(value, default):
    try:
        parsed = float(value)
        return parsed if math.isfinite(parsed) and parsed > 0 else default
    except (TypeError, ValueError):
        return default


def _release_runtime(key, runtime):
    with _cache_lock:
        runtime.references -= 1
        if runtime.references == 0:
            runtime.stop()
            _cache.pop(key, None)


def _stop_owned_process(process, descendants):
    """Snapshot actual children before signalling; psutil guards recycled identities."""
    import psutil
    from tools.environments.local import _kill_known_pids

    if process.poll() is None:
        try:
            known = {child.pid for child in descendants}
            descendants.extend(child for child in psutil.Process(process.pid).children(recursive=True) if child.pid not in known)
        except psutil.NoSuchProcess:
            pass
        with contextlib.suppress(ProcessLookupError):
            process.terminate()
        with contextlib.suppress(subprocess.TimeoutExpired):
            process.wait(timeout=10)
    # Includes PostgreSQL descendants that started their own session. Never discover
    # victims by port, argv, persistent PID file, or a recycled bare process number.
    _kill_known_pids(process, descendants)
    process.wait(timeout=5)
    psutil.wait_procs(descendants, timeout=2)


def _live_descendants(descendants):
    import psutil

    return any(process.is_running() and process.status() != psutil.STATUS_ZOMBIE for process in descendants)
