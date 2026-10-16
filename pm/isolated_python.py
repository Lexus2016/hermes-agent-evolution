"""Managed interpreters for opt-in isolated environments, never the core runtime.

The public minor selector maps to an exact CPython release. PM's pinned uv owns
its official download catalog and integrity checks; no ambient Python, custom
catalog, mirror, user bin link or registry registration participates.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess

from pm.package import InstallError

_SUPPORTED = {"3.12": "3.12.13"}


def exact_version(selector: str) -> str:
    if selector not in _SUPPORTED:
        raise ValueError(f"unsupported isolated Python selector: {selector!r}")
    return _SUPPORTED[selector]


def interpreter_root() -> Path:
    from hermes_constants import get_default_hermes_root
    return get_default_hermes_root() / "cache" / "pm-isolated-python"


def probe(python: Path) -> dict | None:
    """Read actual interpreter identity without importing site or writing bytecode."""
    try:
        result = subprocess.run(
            [str(python), "-I", "-S", "-B", "-c",
             "import json,sys; print(json.dumps({'version': '.'.join(map(str,sys.version_info[:3])), "
             "'implementation': sys.implementation.name, 'base': sys._base_executable}))"],
            capture_output=True, text=True, encoding="utf-8", timeout=15, check=False)
        value = json.loads(result.stdout) if result.returncode == 0 else None
        if not isinstance(value, dict) or value.get("implementation") != "cpython":
            return None
        if not isinstance(value.get("version"), str) or not isinstance(value.get("base"), str):
            return None
        return value
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None


def valid(python: Path, selector: str, identity: dict) -> bool:
    """Reject stale, replaced or wrong-version selections, including old core venvs."""
    version = exact_version(selector)
    if not isinstance(identity, dict) or identity.get("version") != version:
        return False
    try:
        base = Path(identity["interpreter"])
        if not base.resolve().is_relative_to(interpreter_root().resolve()):
            return False
        if hashlib.sha256(base.read_bytes()).hexdigest() != identity["executable_sha256"]:
            return False
    except (KeyError, OSError, TypeError, ValueError):
        return False
    actual = probe(python)
    return bool(actual and actual["version"] == version
                and Path(actual["base"]).resolve() == base.resolve())


def resolve(selector: str, *, realize: bool = False, explicit: bool = False) -> tuple[Path, dict] | None:
    """Use only PM-pinned uv and its built-in verified managed-download catalog."""
    from pm._uv import _toolchain
    from pm.environment import _base_environment

    version = exact_version(selector)
    tools = _toolchain(realize=realize, explicit=explicit)
    if tools is None:
        if realize:
            raise InstallError("python", "PM's pinned toolchain is unavailable")
        return None
    uv = tools[0]
    root = interpreter_root().resolve()
    env = _base_environment(None)
    env.update(UV_PYTHON_INSTALL_DIR=str(root), UV_PYTHON_DOWNLOADS="never")

    def run(args: list[str], *, install: bool = False):
        child_env = dict(env)
        if install:
            child_env["UV_PYTHON_DOWNLOADS"] = "manual"
        try:
            return subprocess.run([str(uv), *args, "--no-config", "--no-cache"],
                                  env=child_env, capture_output=True, text=True, encoding="utf-8",
                                  timeout=600 if install else 30, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise InstallError("python", f"managed Python {version} unavailable: {exc}") from exc

    requested = f"cpython-{version}"
    catalog = run(["python", "list", requested, "--only-downloads", "--output-format", "json"])
    try:
        rows = json.loads(catalog.stdout)
        row = next(r for r in rows if r.get("version") == version
                   and r.get("implementation") == "cpython" and r.get("variant") == "default")
        url = row["url"]
        if not isinstance(url, str):
            raise ValueError("missing official distribution URL")
        if not (url.startswith("https://releases.astral.sh/github/python-build-standalone/releases/download/")
                or url.startswith("https://github.com/astral-sh/python-build-standalone/releases/download/")):
            raise ValueError("not an official managed distribution")
    except (ValueError, TypeError, KeyError, StopIteration) as exc:
        raise InstallError("python", f"verified managed Python {version} is unavailable") from exc
    found = run(["python", "find", requested, "--managed-python", "--no-python-downloads", "--no-project"])
    if found.returncode and realize:
        root.mkdir(parents=True, exist_ok=True)
        installed = run(["python", "install", requested, "--install-dir", str(root), "--no-bin", "--no-registry"], install=True)
        if installed.returncode:
            raise InstallError("python", f"managed Python {version} install failed: {installed.stderr[-600:]}")
        found = run(["python", "find", requested, "--managed-python", "--no-python-downloads", "--no-project"])
    if found.returncode:
        if realize:
            raise InstallError("python", f"managed Python {version} is unavailable")
        return None
    python = Path(found.stdout.strip()).resolve()
    if not python.is_file() or not python.is_relative_to(root):
        raise InstallError("python", "managed interpreter is outside PM's isolated store")
    identity = {"interpreter": str(python), "version": version, "artifact": url, "catalog_key": row["key"],
                "executable_sha256": hashlib.sha256(python.read_bytes()).hexdigest()}
    if not valid(python, selector, identity):
        raise InstallError("python", f"managed interpreter does not match CPython {version}")
    return python, identity
