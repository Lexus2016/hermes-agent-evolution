#!/usr/bin/env python3
"""Safe Python package installation for PEP 668 externally-managed environments.

When the active Python is PEP 668 managed (Debian/Ubuntu marker file),
naive ``pip install`` fails immediately.  This module provides a transparent
rewrite for ``terminal_tool`` and ``execute_code`` so that model-generated
``pip install`` commands keep working without requiring the model to
remember platform-specific workarounds.

Public API:
- :func:`is_pep668_active`: bool probe
- :func:`rewrite_pip_command`: rewrite a shell command string
- :func:`ensure_transient_venv`: create / return a transient venv path
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

# Stable prefix so repeated pip install calls reuse the same transient venv
# within a single process lifetime (cheap — no duplicate creations).
_TRANSIENT_VENV_DIR: Optional[str] = None


def _run(cmd: list[str], timeout: float = 60.0, env: Optional[dict] = None) -> tuple[int, str, str]:
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, check=False, env=env
        )
        return result.returncode, (result.stdout or "").strip(), (result.stderr or "").strip()
    except FileNotFoundError:
        return -1, "", "not found"
    except subprocess.TimeoutExpired:
        return -1, "", "timeout"


def is_pep668_active() -> bool:
    """True when ``sys.executable``'s stdlib directory contains EXTERNALLY-MANAGED."""
    stdlib = os.path.dirname(os.__file__)
    marker = os.path.join(stdlib, "EXTERNALLY-MANAGED")
    return os.path.exists(marker)


def has_uv() -> bool:
    return shutil.which("uv") is not None


def _uv_python_flag() -> Optional[str]:
    """Return a ``--python=...`` flag targeting the current interpreter, or None."""
    uv = shutil.which("uv")
    if not uv:
        return None
    rc, _, _ = _run([uv, "python", "find", sys.executable])
    if rc == 0:
        return f"--python={sys.executable}"
    return None


def ensure_transient_venv() -> Tuple[str, bool]:
    """Create (or reuse) a transient venv and return ``(venv_dir, created_now)``.

    The venv is created under ``/tmp`` with a stable name derived from the
    active interpreter path so that multiple calls within a session reuse it.
    """
    global _TRANSIENT_VENV_DIR
    if _TRANSIENT_VENV_DIR is not None and os.path.isdir(_TRANSIENT_VENV_DIR):
        logger.debug("Reusing transient venv %s", _TRANSIENT_VENV_DIR)
        return _TRANSIENT_VENV_DIR, False

    # Unique but deterministic per-interpreter path
    import hashlib
    suffix = hashlib.sha256(sys.executable.encode()).hexdigest()[:12]
    venv_dir = os.path.join(tempfile.gettempdir(), f"hermes_pep668_venv_{suffix}")

    if os.path.isdir(venv_dir):
        pip = os.path.join(venv_dir, "bin", "pip")
        if os.path.isfile(pip):
            _TRANSIENT_VENV_DIR = venv_dir
            return venv_dir, False

    rc, out, err = _run([sys.executable, "-m", "venv", venv_dir], timeout=60)
    if rc != 0:
        logger.warning("Failed to create transient venv at %s: %s", venv_dir, err or out)
        return "", False

    _TRANSIENT_VENV_DIR = venv_dir
    logger.info("Created transient venv at %s", venv_dir)
    return venv_dir, True


def _rewrite_to_uv(cmd: str) -> Optional[str]:
    """Rewrite ``pip install ...`` to ``uv pip install --python=... ...``.

    Returns None when uv is not available or the command doesn't match.
    """
    if not has_uv():
        return None
    flag = _uv_python_flag()
    if not flag:
        return None

    # Match pip install ...  (allow leading whitespace or compound commands)
    m = re.search(r"(?:^|[;&|]+)\s*pip\s+install\s+(.+?)(?:[;&|]|$)", cmd)
    if not m:
        return None
    args = m.group(1).strip()
    replacement = f"uv pip install {flag} {args}"
    return cmd[:m.start()] + replacement + cmd[m.end():]


def _rewrite_to_transient_venv(cmd: str) -> Optional[str]:
    """Rewrite ``pip install ...`` to use transient-venv pip.

    Returns None when venv creation fails or the command doesn't match.
    """
    venv_dir, _ = ensure_transient_venv()
    if not venv_dir:
        return None

    pip_bin = os.path.join(venv_dir, "bin", "pip")
    if sys.platform == "win32":
        pip_bin = os.path.join(venv_dir, "Scripts", "pip.exe")
    if not os.path.isfile(pip_bin):  # pragma: no cover
        return None

    m = re.search(r"(?:^|[;&|]+)\s*pip\s+install\s+(.+?)(?:[;&|]|$)", cmd)
    if not m:
        return None
    args = m.group(1).strip()
    replacement = f"{pip_bin} install {args}"
    return cmd[:m.start()] + replacement + cmd[m.end():]


def rewrite_pip_command(command: str) -> Optional[str]:
    """Rewrite a shell command to avoid PEP 668 ``pip install`` failures.

    Returns a rewritten command string when:
      - the command contains a bare ``pip install`` invocation,
      - PEP 668 is active,
      - the command does NOT already contain ``--break-system-packages``,
        ``--user``, ``pip uninstall``, or ``pip remove``.

    Returns None when no rewrite is needed or no viable fallback exists.
    """
    if not is_pep668_active():
        return None

    # Only touch install commands
    if not re.search(r"(?:^|[;&|]+)\s*pip\s+install\b", command):
        return None

    # Skip if the user already opted out of PEP 668
    if "--break-system-packages" in command or "--user" in command:  # pragma: no cover
        return None

    # Try uv first
    rewritten = _rewrite_to_uv(command)
    if rewritten:
        logger.info("Rewrote pip install → uv pip install for PEP 668")
        return rewritten

    # Fallback to transient venv
    rewritten = _rewrite_to_transient_venv(command)
    if rewritten:
        logger.info("Rewrote pip install → transient venv pip for PEP 668")
        return rewritten

    return None


def get_install_hint() -> str:
    """Return a concise, actionable hint for the system prompt when PEP 668 is active.

    The hint is ONE line so it doesn't dominate the prompt.
    """
    if not is_pep668_active():
        return ""
    if has_uv():
        flag = _uv_python_flag() or "--python=<interpreter>"
        return (
            f"PEP 668=yes (externally-managed).  Use `uv pip install {flag} <pkg>` "
            f"instead of `pip install`.  `uv pip install --python={sys.executable} <pkg>` is preferred."
        )
    venv_dir, ok = ensure_transient_venv()
    if ok or venv_dir:
        pip = os.path.join(venv_dir, "bin", "pip")
        return (
            f"PEP 668=yes (externally-managed).  Use `{pip} install <pkg>` "
            f"instead of `pip install`; or first create a venv: "
            f"`python3 -m venv /tmp/venv && /tmp/venv/bin/pip install <pkg>`."
        )
    return "PEP 668=yes (externally-managed).  pip install will fail — create a venv first."
