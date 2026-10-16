"""Opt-in ABI selection retains core defaults and rejects stale interpreter generations."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import subprocess
from threading import Barrier

import pytest

from pm import client, isolated_python, operations
from pm.package import InstallError


@pytest.fixture
def selected_python(tmp_path, monkeypatch):
    store = tmp_path / "interpreters"
    store.mkdir()
    base = store / "python"
    base.write_bytes(b"managed cpython fixture")
    identity = {"interpreter": str(base), "version": "3.12.13", "artifact": "official-fixture",
                "executable_sha256": hashlib.sha256(base.read_bytes()).hexdigest()}
    monkeypatch.setattr(isolated_python, "interpreter_root", lambda: store)
    versions = {}
    monkeypatch.setattr(isolated_python, "probe", lambda p: {
        "version": versions.get(str(p), "3.12.13"), "base": str(base), "implementation": "cpython"})
    monkeypatch.setattr(isolated_python, "resolve", lambda *a, **k: (base, dict(identity)))
    from pm import _uv
    monkeypatch.setattr(_uv, "_toolchain", lambda **k: (base, base))
    monkeypatch.setattr("pm.environments.flush_before_selecting", lambda: None)
    built = []

    def build(**kwargs):
        out = kwargs["out"]
        python = operations._python(out)
        python.parent.mkdir(parents=True)
        python.touch()
        (out / "pyvenv.cfg").write_text("version = 3.12.13\n", encoding="utf-8")
        built.append(kwargs)
        return python

    monkeypatch.setattr(operations, "build_environment", build)
    return tmp_path / "environment", base, versions, built


def test_client_preserves_default_payload_and_forwards_selector(monkeypatch):
    calls = []
    monkeypatch.setattr(client, "_python_operation", lambda op, args: calls.append((op, args)) or "/python")
    client.ensure_environment("side", ["example==1"])
    assert "python_version" not in calls[-1][1]
    client.ensure_environment("side", ["example==1"], python_version="3.12")
    assert calls[-1][1]["python_version"] == "3.12"


def test_selected_abi_is_validated_and_cached(selected_python):
    root, base, versions, built = selected_python
    args = dict(root=root, explicit=True, python_version="3.12")
    python = operations.ensure_environment("side", ["example==1"], **args)
    assert built[0]["python"] == base
    assert 'requires-python = ">=3.12,<3.13"' in (python.parent.parent.parent / "pyproject.toml").read_text()
    assert operations.ensure_environment("side", ["example==1"], **args) == python
    assert operations.environment_python("side", root=root, python_version="3.12") == python
    assert len(built) == 1
    versions[str(python)] = "3.14.7"
    assert operations.environment_python("side", root=root, python_version="3.12") is None
    replacement = operations.ensure_environment("side", ["example==1"], **args)
    assert replacement != python and python.is_file()
    assert len(built) == 2


def test_default_generation_is_not_a_selected_abi(selected_python):
    root, _, _, built = selected_python
    default = operations.ensure_environment("side", ["example==1"], root=root, explicit=True)
    assert operations.ensure_environment("side", ["example==1"], root=root, explicit=True) == default
    assert "python_version" not in json.loads((root / "active.json").read_text())
    assert operations.environment_python("side", root=root, python_version="3.12") is None
    other = operations.ensure_environment("side", ["example==1"], root=root, explicit=True, python_version="3.12")
    assert other != default and len(built) == 2


def test_unavailable_selected_interpreter_never_falls_back(selected_python, monkeypatch):
    root, _, _, built = selected_python
    monkeypatch.setattr(isolated_python, "resolve", lambda *a, **k: None)
    with pytest.raises(InstallError, match="requested managed interpreter"):
        operations.ensure_environment("side", ["example==1"], root=root, explicit=True, python_version="3.12")
    assert not built and not (root / "active.json").exists()


def test_invalid_build_preserves_previous_selection(selected_python):
    root, _, _versions, built = selected_python
    first = operations.ensure_environment("side", ["example==1"], root=root, explicit=True, python_version="3.12")
    before = (root / "active.json").read_bytes()
    # All newly built executables report an incompatible interpreter.
    class WrongVersion(dict):
        def get(self, key, default=None):
            return "3.12.13" if key == str(first) else "3.14.7"
    from unittest.mock import patch
    with patch.object(isolated_python, "probe", lambda p: {
            "version": WrongVersion().get(str(p)), "base": str(built[0]["python"]), "implementation": "cpython"}):
        with pytest.raises(InstallError, match="wrong interpreter identity"):
            operations.ensure_environment("side", ["example==2"], root=root, explicit=True, python_version="3.12")
    assert (root / "active.json").read_bytes() == before
    assert first.is_file()


def test_concurrent_ensures_publish_one_generation(selected_python):
    root, _, _, built = selected_python
    barrier = Barrier(2)
    def ensure():
        barrier.wait(timeout=10)
        return operations.ensure_environment("side", ["example==1"], root=root, explicit=True, python_version="3.12")
    with ThreadPoolExecutor(max_workers=2) as pool:
        a, b = list(pool.map(lambda _: ensure(), range(2)))
    assert a == b and len(built) == 1


def test_unsupported_selector_does_not_create_state(tmp_path):
    with pytest.raises(ValueError, match="unsupported"):
        operations.ensure_environment("side", ["example==1"], root=tmp_path / "absent", python_version="3.11")
    assert not (tmp_path / "absent").exists()


def test_read_only_lookup_never_resolves_or_installs(selected_python, monkeypatch):
    root, _, _, _ = selected_python
    python = operations.ensure_environment("side", ["example==1"], root=root, explicit=True, python_version="3.12")
    before = (root / "active.json").read_bytes()
    monkeypatch.setattr(isolated_python, "resolve", lambda *a, **k: pytest.fail("lookup acquired interpreter"))
    assert operations.environment_python("side", root=root, python_version="3.12") == python
    assert (root / "active.json").read_bytes() == before


@pytest.mark.parametrize("realize", [False, True])
def test_managed_transport_is_private_and_never_downloads_on_probe(tmp_path, monkeypatch, realize):
    import os
    from pm import _uv

    root = tmp_path / "managed"
    python = root / "cpython" / "python"
    uv = tmp_path / "uv"
    monkeypatch.setattr(isolated_python, "interpreter_root", lambda: root)
    monkeypatch.setattr(_uv, "_toolchain", lambda **kwargs: (uv, tmp_path / "core-python"))
    monkeypatch.setenv("UV_PYTHON_INSTALL_MIRROR", "https://untrusted.invalid")
    monkeypatch.setenv("UV_PYTHON_DOWNLOADS_JSON_URL", "https://untrusted.invalid/catalog")
    monkeypatch.setenv("UV_PYTHON", "/system/python")
    monkeypatch.setenv("UV_PYTHON_INSTALL_DIR", "/outside")
    before = dict(os.environ)
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        if command[0] == str(python):
            return subprocess.CompletedProcess(command, 0, stdout=json.dumps({
                "version": "3.12.13", "implementation": "cpython", "base": str(python)}))
        env = kwargs["env"]
        assert env["UV_PYTHON_INSTALL_DIR"] == str(root)
        assert not {"UV_PYTHON", "UV_PYTHON_INSTALL_MIRROR", "UV_PYTHON_DOWNLOADS_JSON_URL"} & env.keys()
        assert "--no-config" in command and "--no-cache" in command
        if command[1:3] == ["python", "list"]:
            return subprocess.CompletedProcess(command, 0, stdout=json.dumps([{
                "version": "3.12.13", "implementation": "cpython", "variant": "default",
                "key": "cpython-3.12.13-fixture", "url":
                "https://releases.astral.sh/github/python-build-standalone/releases/download/fixture/python.tar.gz"}]))
        if command[1:3] == ["python", "install"]:
            assert realize and "--no-bin" in command and "--no-registry" in command
            python.parent.mkdir(parents=True)
            python.write_bytes(b"verified fixture")
            return subprocess.CompletedProcess(command, 0, stderr="")
        assert "--managed-python" in command and "--no-python-downloads" in command
        return subprocess.CompletedProcess(command, 0 if python.exists() else 1, stdout=str(python))

    monkeypatch.setattr(subprocess, "run", run)
    result = isolated_python.resolve("3.12", realize=realize, explicit=True)
    assert dict(os.environ) == before
    if realize:
        assert result[0] == python
        assert result[1]["version"] == "3.12.13"
        assert result[1]["catalog_key"] == "cpython-3.12.13-fixture"
    else:
        assert result is None and not root.exists()
        assert not any(command[1:3] == ["python", "install"] for command in calls)


def test_unverified_or_unsupported_catalog_fails_closed(tmp_path, monkeypatch):
    from pm import _uv
    monkeypatch.setattr(_uv, "_toolchain", lambda **kwargs: (tmp_path / "uv", tmp_path / "python"))
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout="[]"))
    with pytest.raises(InstallError, match="verified managed Python 3.12.13 is unavailable"):
        isolated_python.resolve("3.12", realize=True, explicit=True)


def test_replaced_base_executable_invalidates_read_only_selection(selected_python):
    root, base, _, _ = selected_python
    operations.ensure_environment("side", ["example==1"], root=root, explicit=True, python_version="3.12")
    base.write_bytes(b"replaced interpreter at the same path")
    assert operations.environment_python("side", root=root, python_version="3.12") is None


@pytest.mark.parametrize("configured", [False, True])
def test_worker_fixture_forwards_only_explicit_supported_cache(tmp_path, monkeypatch, configured):
    from types import SimpleNamespace
    from tests.pm import _fixtures
    from pm import runtime_stage
    cache = tmp_path / "verified-cache"
    if configured:
        monkeypatch.setenv("UV_CACHE_DIR", str(cache))
    else:
        monkeypatch.delenv("UV_CACHE_DIR", raising=False)
    calls = []
    monkeypatch.setattr(_fixtures.shutil, "which", lambda name: str(tmp_path / "uv"))
    monkeypatch.setattr(_fixtures, "_run", lambda *a, **k: None)
    monkeypatch.setattr(runtime_stage, "stage_runtime", lambda *a, **k: calls.append((a, k)) or tmp_path / "python")
    _fixtures.isolated_python.__wrapped__(SimpleNamespace(mktemp=lambda name: tmp_path / name))
    assert calls[0][1] == {"cache": cache if configured else None}
    assert calls[0][0][2] == tmp_path / "pm-python"
