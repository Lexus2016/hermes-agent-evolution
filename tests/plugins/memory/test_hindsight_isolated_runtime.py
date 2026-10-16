"""Behavioral contracts for isolated PM runtime and owned local subprocess lifecycle."""
import asyncio
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from plugins.memory.hindsight import daemon_bridge as bridge
from plugins.memory.hindsight import isolated_runtime as runtime


def test_pm_installs_exact_isolated_stack(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr("pm.ensure_environment", lambda *a, **k: calls.append((a, k)) or tmp_path)
    assert runtime.install_runtime() == tmp_path
    assert calls == [(("hindsight-embedded", runtime.REQUIREMENTS), {"explicit": True, "python_version": "3.12"})]
    assert runtime.REQUIREMENTS == ("hindsight-all==0.6.1", "hindsight-embed==0.6.1", "hindsight-client==0.6.1",
                                    "pg0-embedded==0.15.2", "psutil==7.2.2")


def test_missing_runtime_probe_does_not_install(monkeypatch):
    monkeypatch.setattr("pm.environment_python", lambda *a, **k: None)
    monkeypatch.setattr("pm.ensure_environment", lambda *a, **k: pytest.fail("probe installed dependencies"))
    available, reason = runtime.probe_runtime()
    assert not available
    assert "hermes memory setup" in reason


def test_default_identity_preserves_legacy(monkeypatch, tmp_path):
    monkeypatch.setattr(runtime, "profile_name_for_home", lambda home: "default")
    assert runtime.database_identity({"profile": "existing"}, home=tmp_path) == "hindsight-embed-existing"


def test_ambiguous_legacy_database_requires_operator_choice(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(runtime, "profile_name_for_home", lambda home: "secondary")
    legacy = tmp_path / ".pg0" / "instances" / "hindsight-embed-hermes"
    legacy.mkdir(parents=True)
    data = legacy / "synthetic-memory"
    data.write_bytes(b"legacy synthetic memory\x00")
    with pytest.raises(RuntimeError, match="cannot adopt it automatically"):
        runtime.database_identity({}, home=tmp_path / "profile-a")
    a = runtime.database_identity({"embedded_database_choice": "fresh"}, home=tmp_path / "profile-a")
    b = runtime.database_identity({"embedded_database_choice": "fresh"}, home=tmp_path / "profile-b")
    assert a != b
    assert data.read_bytes() == b"legacy synthetic memory\x00"
    assert sorted(legacy.iterdir()) == [data]


def test_fresh_profile_identity_is_stable_and_isolated(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(runtime, "profile_name_for_home", lambda home: "secondary")
    a = runtime.database_identity({}, home=tmp_path / "a")
    assert runtime.database_identity({}, home=tmp_path / "a") == a
    assert runtime.database_identity({}, home=tmp_path / "b") != a
    with pytest.raises(ValueError):
        runtime.database_identity({"profile": "../../escape"}, home=tmp_path)


@pytest.fixture
def child_script(tmp_path):
    script = tmp_path / "synthetic_daemon.py"
    script.write_text('''import json,sys,threading,os,signal
from http.server import HTTPServer, BaseHTTPRequestHandler
settings=json.loads(sys.stdin.readline())
key=settings["environment"]["HINDSIGHT_API_TENANT_API_KEY"]
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200 if self.path=="/health" or self.headers.get("Authorization")=="Bearer "+key else 401)
        self.end_headers()
    def log_message(self,*args): pass
server=HTTPServer(("127.0.0.1",0),Handler)
def send(event,**kwargs):
    print(json.dumps(dict(event=event,generation=settings["generation"],**kwargs)),flush=True)
def shutdown(*args):
    send("clean")
    raise SystemExit(0)
signal.signal(signal.SIGTERM,shutdown)
send("ready",url="http://127.0.0.1:"+str(server.server_port))
threading.Thread(target=lambda:(sys.stdin.read(),server.shutdown()),daemon=True).start()
server.serve_forever()
''')
    return script


def make_daemon(monkeypatch, tmp_path, script, timeout=3):
    daemon = bridge.OwnedDaemon(tmp_path, "synthetic-db", {"HINDSIGHT_API_LLM_API_KEY": "synthetic-provider"}, 0, timeout)
    monkeypatch.setattr(daemon, "_command", lambda: [sys.executable, "-I", str(script)])
    return daemon


def test_real_child_health_restart_and_shutdown(monkeypatch, tmp_path, child_script):
    daemon = make_daemon(monkeypatch, tmp_path, child_script)
    try:
        url = daemon.ensure_started()
        process = daemon.process
        assert url.startswith("http://127.0.0.1:")
        assert daemon.ensure_started() == url
        assert daemon.process is process
        assert "synthetic-provider" not in " ".join(process.args)
        assert daemon.api_key not in " ".join(process.args)
        process.terminate()
        process.wait(timeout=5)
        daemon.ensure_started()
        replacement = daemon.process
        assert replacement.pid != process.pid
    finally:
        daemon.stop()
    assert replacement.poll() is not None
    assert daemon._file_lock is None


def test_startup_timeout_reaps_only_owned_child(monkeypatch, tmp_path):
    script = tmp_path / "never_ready.py"
    script.write_text("import sys\nsys.stdin.readline()\nsys.stdin.read()\n")
    daemon = make_daemon(monkeypatch, tmp_path, script, timeout=0.1)
    seen = []
    original = bridge.subprocess.Popen
    def capture(*args, **kwargs):
        process = original(*args, **kwargs)
        seen.append(process)
        return process
    monkeypatch.setattr(bridge.subprocess, "Popen", capture)
    with pytest.raises(RuntimeError, match="cleanup could not be proven"):
        daemon.ensure_started()
    assert len(seen) == 1
    assert seen[0].poll() is not None
    assert daemon._file_lock is not None
    assert daemon._ledger.exists()
    daemon._release_lock()


def test_invalid_endpoint_never_adopted(monkeypatch, tmp_path, child_script):
    source = child_script.read_text().replace('url="http://127.0.0.1:"+str(server.server_port)',
                                            'url="https://example.invalid:443"')
    child_script.write_text(source)
    daemon = make_daemon(monkeypatch, tmp_path, child_script)
    with pytest.raises(RuntimeError, match="invalid loopback endpoint"):
        daemon.ensure_started()
    assert daemon.process is None
    assert daemon._clean_receipt
    assert not daemon._ledger.exists()


def test_profile_lock_refuses_second_owner(monkeypatch, tmp_path, child_script):
    first = make_daemon(monkeypatch, tmp_path, child_script)
    second = make_daemon(monkeypatch, tmp_path, child_script)
    try:
        first.ensure_started()
        with pytest.raises(RuntimeError, match="restart is blocked"):
            second.ensure_started()
        assert first.process.poll() is None
        assert second.process is None
    finally:
        first.stop()


def test_async_client_leases_keep_sibling_alive(monkeypatch, tmp_path):
    monkeypatch.setattr(bridge, "get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(bridge, "runtime_python", lambda: Path(sys.executable))
    monkeypatch.setattr(bridge, "database_identity", lambda *a, **k: "lease-db")
    kwargs = dict(profile="hermes", llm_provider="openai", llm_api_key="synthetic",
                  llm_model="fake", idle_timeout=0)
    first = bridge.ManagedEmbeddedClient(**kwargs)
    second = bridge.ManagedEmbeddedClient(**kwargs)
    assert first._runtime is second._runtime
    stopped = []
    monkeypatch.setattr(first._runtime, "stop", lambda: stopped.append(True))
    first._client = SimpleNamespace(aclose=AsyncMock())
    inner = first._client
    async def close():
        await first.aclose()
        assert stopped == []
        await second.aclose()
        await second.aclose()
    asyncio.run(close())
    inner.aclose.assert_awaited_once()
    assert stopped == [True]
    assert first._key not in bridge._cache


def test_real_daemons_reject_sibling_and_missing_tokens(monkeypatch, tmp_path, child_script):
    import urllib.error
    import urllib.request

    first = make_daemon(monkeypatch, tmp_path / "a", child_script)
    second = make_daemon(monkeypatch, tmp_path / "b", child_script)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        first.ensure_started()
        second.ensure_started()
        assert first.url != second.url
        assert first.api_key != second.api_key
        for token in (None, first.api_key):
            headers = {"Authorization": f"Bearer {token}"} if token else {}
            with pytest.raises(urllib.error.HTTPError) as error:
                opener.open(urllib.request.Request(second.url + "/v1/default/banks", headers=headers), timeout=2)
            assert error.value.code == 401
        assert second._healthy()
    finally:
        first.stop()
        second.stop()


def test_child_environment_only_receives_scoped_hindsight_settings(monkeypatch, tmp_path):
    monkeypatch.setenv("AUXILIARY_API_KEY", "synthetic-unrelated")
    monkeypatch.setenv("HINDSIGHT_API_LLM_API_KEY", "synthetic-launch-profile")
    monkeypatch.setenv("HINDSIGHT_API_URL", "https://launch-profile.invalid")
    capture = tmp_path / "capture.json"
    script = tmp_path / "inspect_env.py"
    script.write_text('''import json,os,sys
from pathlib import Path
settings=json.loads(sys.stdin.readline())
Path(__file__).with_name("capture.json").write_text(json.dumps({
"provided":settings["environment"]["HINDSIGHT_API_LLM_API_KEY"],
"inherited":[os.environ.get(k) for k in ("AUXILIARY_API_KEY","HINDSIGHT_API_LLM_API_KEY","HINDSIGHT_API_URL")]}))
print('{"url":"https://invalid.invalid:443"}',flush=True)
sys.stdin.read()
''')
    daemon = make_daemon(monkeypatch, tmp_path, script)
    with pytest.raises(RuntimeError, match="cleanup could not be proven"):
        daemon.ensure_started()
    assert json.loads(capture.read_text()) == {"provided": "synthetic-provider", "inherited": [None, None, None]}
    assert daemon._ledger.exists()
    daemon._release_lock()


def test_operations_namespace_uses_async_host_client(monkeypatch, tmp_path):
    monkeypatch.setattr(bridge, "get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(bridge, "runtime_python", lambda: Path(sys.executable))
    monkeypatch.setattr(bridge, "database_identity", lambda *a, **k: "operations-db")
    client = bridge.ManagedEmbeddedClient(profile="hermes", llm_provider="openai", llm_api_key="synthetic",
                                          llm_model="fake", idle_timeout=0)
    get_status = AsyncMock(return_value={"status": "completed"})
    inner = SimpleNamespace(operations=SimpleNamespace(get_operation_status=get_status))
    monkeypatch.setattr(client, "_prepare", AsyncMock(return_value=inner))
    async def run():
        try:
            assert await client.operations.get_operation_status(bank_id="b", operation_id="o") == {"status": "completed"}
        finally:
            await client.aclose()
    asyncio.run(run())
    get_status.assert_awaited_once_with(bank_id="b", operation_id="o")


@pytest.mark.parametrize("scenario", ["clean", "crash", "rapid_exit_zero"])
def test_owned_descendants_in_isolated_subreaper(monkeypatch, tmp_path, child_script, scenario):
    """OS reparenting stays inside a disposable process; pytest's live guard remains enabled."""
    if sys.platform != "linux":
        pytest.skip("Linux process-reparenting contract")
    import subprocess
    import textwrap

    code = child_script.read_text().replace('server=HTTPServer', '''import subprocess
from pathlib import Path
child=subprocess.Popen([sys.executable,"-c","import threading; threading.Event().wait()"],start_new_session=True)
Path(__file__).with_name("descendant.pid").write_text(str(child.pid))
server=HTTPServer''')
    if scenario == "clean":
        code = code.replace('    send("clean")', '    child.terminate(); child.wait(timeout=5)\n    send("clean")')
    if scenario == "rapid_exit_zero":
        code = code.replace('server=HTTPServer', 'raise SystemExit(0)\nserver=HTTPServer')
    child_script.write_text(code)
    harness = textwrap.dedent('''
        import ctypes,os,sys,subprocess,psutil
        from pathlib import Path
        from plugins.memory.hindsight.daemon_bridge import OwnedDaemon
        home,script,scenario=Path(sys.argv[1]),Path(sys.argv[2]),sys.argv[3]
        libc=ctypes.CDLL(None,use_errno=True)
        previous=ctypes.c_int()
        assert libc.prctl(37,ctypes.byref(previous),0,0,0)==0
        assert libc.prctl(36,1,0,0,0)==0
        foreign=subprocess.Popen([sys.executable,"-c","import threading; threading.Event().wait()"])
        daemon=OwnedDaemon(home,"synthetic-db",{},0,3)
        daemon._command=lambda:[sys.executable,"-I",str(script)]
        try:
            if scenario=="rapid_exit_zero":
                try: daemon.ensure_started()
                except RuntimeError as exc: assert "cleanup could not be proven" in str(exc)
                else: raise AssertionError("ambiguous startup accepted")
            else:
                daemon.ensure_started()
                if scenario=="crash":
                    daemon.process.kill(); daemon.process.wait(timeout=5)
                try: daemon.stop()
                except RuntimeError as exc:
                    assert scenario=="crash" and "cleanup could not be proven" in str(exc)
                else: assert scenario=="clean"
            descendant_pid=int((home/"descendant.pid").read_text())
            remaining=[p for p in psutil.Process().children(recursive=True) if p.pid==descendant_pid]
            if scenario=="rapid_exit_zero":
                assert remaining and remaining[0].is_running()
            else:
                assert not remaining or remaining[0].status()==psutil.STATUS_ZOMBIE
            if scenario!="clean":
                assert daemon._ledger.exists() and daemon._file_lock is not None
                replacement=OwnedDaemon(home,"synthetic-db",{},0,3)
                try: replacement.ensure_started()
                except RuntimeError as exc: assert "restart is blocked" in str(exc)
                else: raise AssertionError("unresolved generation restarted")
            else:
                assert not daemon._ledger.exists() and daemon._file_lock is None
            assert foreign.poll() is None
        finally:
            for child in psutil.Process().children(recursive=True):
                child.kill()
            psutil.wait_procs(psutil.Process().children(recursive=True),timeout=3)
            daemon._release_lock()
            assert libc.prctl(36,previous.value,0,0,0)==0
        print("owned lifecycle verified")
    ''')
    result = subprocess.run([sys.executable, "-c", harness, str(tmp_path), str(child_script), scenario],
                            capture_output=True, text=True, timeout=20, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "owned lifecycle verified" in result.stdout


def test_new_runtime_generation_is_not_reused_by_new_client(monkeypatch, tmp_path):
    monkeypatch.setattr(bridge, "get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(bridge, "database_identity", lambda *a, **k: "generation-db")
    selected = [tmp_path / "generation-a" / "python"]
    monkeypatch.setattr(bridge, "runtime_python", lambda: selected[0])
    kwargs = dict(profile="hermes", llm_provider="openai", llm_api_key="synthetic",
                  llm_model="fake", idle_timeout=0)
    first = bridge.ManagedEmbeddedClient(**kwargs)
    selected[0] = tmp_path / "generation-b" / "python"
    second = bridge.ManagedEmbeddedClient(**kwargs)
    assert first._runtime is not second._runtime
    assert first._runtime.python != second._runtime.python
    async def close():
        await first.aclose()
        await second.aclose()
    asyncio.run(close())


@pytest.mark.parametrize("provided", [None, "synthetic-wrong", "synthetic-correct"])
def test_launcher_private_nonce_checks_real_process(monkeypatch, provided):
    import subprocess

    import psutil

    from plugins.memory.hindsight.daemon_launcher import _verify_owned_process

    env = dict(os.environ)
    env.pop("HERMES_HINDSIGHT_OWNER_NONCE", None)
    if provided is not None:
        env["HERMES_HINDSIGHT_OWNER_NONCE"] = provided
    child = subprocess.Popen([sys.executable, "-I", "-c", "import time; time.sleep(30)"], env=env)
    try:
        process = psutil.Process(child.pid)
        if provided == "synthetic-correct":
            _verify_owned_process(process, "synthetic-correct", process.create_time())
        else:
            with pytest.raises(RuntimeError, match="does not belong"):
                _verify_owned_process(process, "synthetic-correct", process.create_time())
        assert child.poll() is None
    finally:
        child.terminate()
        child.wait(timeout=5)


def test_launcher_changed_identity_cannot_be_signalled(monkeypatch):
    from plugins.memory.hindsight.daemon_launcher import _terminate_owned_process

    signals = []
    process = SimpleNamespace(pid=123, create_time=lambda: 10.0,
                              environ=lambda: {"HERMES_HINDSIGHT_OWNER_NONCE": "synthetic"},
                              is_running=lambda: True, status=lambda: "running",
                              terminate=lambda: signals.append("terminated"))
    monkeypatch.setattr("psutil.Process", lambda pid: SimpleNamespace(create_time=lambda: 11.0))
    with pytest.raises(RuntimeError, match="does not belong"):
        _terminate_owned_process(process, "synthetic", 10.0)
    assert not signals


def test_launcher_inaccessible_identity_cannot_be_signalled(monkeypatch):
    import psutil

    from plugins.memory.hindsight.daemon_launcher import _terminate_owned_process

    def denied():
        raise psutil.AccessDenied(123)

    process = SimpleNamespace(pid=123, create_time=lambda: 10.0, environ=denied,
                              is_running=lambda: True, status=lambda: "running",
                              terminate=lambda: pytest.fail("unverified process signalled"))
    with pytest.raises(psutil.AccessDenied):
        _terminate_owned_process(process, "synthetic", 10.0)
