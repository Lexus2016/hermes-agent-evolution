"""Idempotent install + registration of the Turbo-Quant Memory MCP server.

Turbo-Quant Memory (``turbo-memory-mcp``) is one of this fork's headline
features: an efficient, economical out-of-window memory so the agent loses
nothing between sessions. Historically it was only wired up *once*, inside
``setup-hermes.sh`` at install time, with every step piped to
``>/dev/null 2>&1 || true`` — so any failure left a half-state (binary
installed, MCP unregistered) that no ``hermes update`` ever healed.

This module is the single source of truth shared by both the fresh-install
script and ``hermes update``. It is:

* **idempotent** — re-running it when ``mcp_servers.tqmemory`` already exists
  is a no-op;
* **self-healing** — ``hermes update`` calls :func:`reconcile_tqmemory`, so a
  machine that missed registration at install time recovers on the next
  update (including the daily auto-update cron), on every OS;
* **multi-profile** — registers into *every* profile's ``config.yaml`` so the
  memory is available in all sessions, not just the active profile;
* **non-fatal & visible** — never aborts an install/update, but surfaces a
  one-line, actionable message instead of swallowing the reason for a failure;
* **opt-out aware** — honours ``HERMES_NO_TQMEMORY=1`` and the persistent
  ``memory.tqmemory_autoinstall: false`` config flag.

The canonical Hermes-fork schema is the **top-level** ``mcp_servers:`` key
(NOT the Claude-style ``mcp.servers`` — that wrong key crashes the gateway).
Confirmed in ``hermes_cli/mcp_catalog.py``, ``hermes_cli/config.py`` and
``hermes_cli/mcp_startup.py``.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import List, Optional, Tuple

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SERVER_NAME = "tqmemory"
BINARY = "turbo-memory-mcp"
REPO_SPEC = "git+https://github.com/Lexus2016/turbo_quant_memory"
OPT_OUT_ENV = "HERMES_NO_TQMEMORY"

# Why TQMEMORY_MIGRATE_ON_STARTUP=1: when auto-update bumps the tqmemory binary
# to a version with a pending schema migration, the next daemon start applies it
# (after a rolling snapshot) instead of serving a stale schema or dead-locking
# on ``migrate --apply``. Without it, ``uv tool upgrade`` updates the code but
# leaves storage un-migrated.
_SERVER_ENV = {"TQMEMORY_MIGRATE_ON_STARTUP": "1"}

# uv install can pull + build for a minute on a cold machine.
_INSTALL_TIMEOUT = 600
_UPGRADE_TIMEOUT = 180
_VERIFY_TIMEOUT = 20


# ---------------------------------------------------------------------------
# Opt-out / managed gating
# ---------------------------------------------------------------------------

def _truthy(value: Optional[str]) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "on"} if value else False


def opted_out() -> bool:
    """True if the user explicitly disabled tqmemory auto-setup.

    Two channels: the ``HERMES_NO_TQMEMORY`` env var (one-shot, used by
    ``setup-hermes.sh``) and a persistent ``memory.tqmemory_autoinstall: false``
    config flag so an explicit "no" survives across updates.
    """
    if _truthy(os.environ.get(OPT_OUT_ENV)):
        return True
    try:
        from hermes_cli.config import read_raw_config

        memory = (read_raw_config() or {}).get("memory")
        if isinstance(memory, dict) and memory.get("tqmemory_autoinstall") is False:
            return True
    except Exception:
        pass
    return False


def _is_managed() -> bool:
    """Managed (e.g. NixOS) installs control config declaratively — never write."""
    try:
        from hermes_cli.config import is_managed

        return bool(is_managed())
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Binary / uv resolution
# ---------------------------------------------------------------------------

def _candidate_bins() -> List[Path]:
    home = Path.home()
    return [home / ".local" / "bin", home / ".cargo" / "bin"]


def _find_uv() -> Optional[str]:
    found = shutil.which("uv")
    if found:
        return found
    for base in _candidate_bins():
        cand = base / "uv"
        if cand.is_file() and os.access(cand, os.X_OK):
            return str(cand)
    return None


def resolve_binary() -> Optional[str]:
    """Absolute path to the ``turbo-memory-mcp`` shim, or ``None`` if absent.

    Resolves the absolute path explicitly because the gateway/cron process
    PATH does NOT include ``~/.local/bin`` — registering a bare command name
    yields a dead MCP ("No such file or directory") even though it shows
    "enabled".
    """
    found = shutil.which(BINARY)
    if found:
        return str(Path(found).resolve()) if not Path(found).is_symlink() else found
    for base in _candidate_bins():
        cand = base / BINARY
        if cand.exists() and os.access(cand, os.X_OK):
            return str(cand)
    return None


# ---------------------------------------------------------------------------
# Install / upgrade
# ---------------------------------------------------------------------------

def ensure_turbo_memory_installed(quiet: bool = False) -> Optional[str]:
    """Install (or upgrade) ``turbo-memory-mcp`` via uv. Returns its abs path.

    Non-fatal: returns ``None`` (with a visible reason) if uv is missing or the
    install fails, so the caller can decide whether to register.
    """
    uv = _find_uv()
    if not uv:
        _emit(quiet, "ℹ️  'uv' not found — skipping Turbo-Quant Memory. "
                     "Install uv (https://docs.astral.sh/uv/) then re-run.")
        return None

    existing = resolve_binary()
    if existing:
        # Best-effort upgrade; never fail the caller on a network hiccup.
        #
        # rev-pin trap: if a PRIOR install pinned the receipt to a concrete git
        # rev (observed on prod: rev=v0.17.0), `uv tool upgrade` re-resolves to
        # that SAME rev and never jumps to a newer commit — the install stays
        # silently stale. REPO_SPEC is intentionally unpinned (no @rev) so a
        # reinstall floats to the branch HEAD. We try the cheap upgrade first
        # (fast on the common, already-latest case) and only fall back to a
        # `--reinstall` against the unpinned spec when the upgrade reported no
        # change ("Nothing to upgrade" / non-zero) — that re-pins the receipt to
        # the unpinned spec and breaks the rev-pin trap without slowing the
        # normal path.
        up = _run([uv, "tool", "upgrade", BINARY], _UPGRADE_TIMEOUT)
        out = (up.stdout or "") + (up.stderr or "")
        upgrade_had_effect = up.returncode == 0 and "Nothing to upgrade" not in out
        if not upgrade_had_effect:
            # Re-resolve from the unpinned REPO_SPEC to escape a rev-pinned receipt.
            _run([uv, "tool", "install", "--reinstall", REPO_SPEC], _INSTALL_TIMEOUT)
        return resolve_binary() or existing

    _emit(quiet, "🧠 Installing Turbo-Quant Memory MCP (one-time, may take a minute)…")
    res = _run([uv, "tool", "install", REPO_SPEC], _INSTALL_TIMEOUT)
    path = resolve_binary()
    if not path:
        # Surface WHY instead of swallowing it (the historical RC1 bug).
        detail = (res.stderr or res.stdout or "").strip().splitlines()
        tail = detail[-1] if detail else "unknown error"
        _emit(quiet, f"⚠️  Turbo-Quant Memory install failed ({tail}). "
                     f"Manual: uv tool install {REPO_SPEC}")
    return path


# ---------------------------------------------------------------------------
# Registration (multi-profile, canonical mcp_servers schema)
# ---------------------------------------------------------------------------

def _build_entry(tqm_path: str) -> dict:
    # Pin TQMEMORY_PROJECT_ROOT to a STABLE root (HERMES_HOME, fallback ~/.hermes)
    # so turbo_quant_memory derives a single, cwd-independent project_id. Without
    # it the project_id tracks the process cwd and memory fragments into multiple
    # buckets (observed on prod: /root vs /root/.hermes).
    hermes_home = os.path.expanduser(os.environ.get("HERMES_HOME", "~/.hermes"))
    env = dict(_SERVER_ENV)
    env.setdefault("TQMEMORY_PROJECT_ROOT", hermes_home)
    return {
        "command": tqm_path,
        "args": ["serve"],
        "env": env,
        # First semantic_search loads a ~600MB embedding model; re-syncs can be
        # slow. Give this server a generous per-call timeout (read per-server by
        # tools/mcp_tool.py) without touching the global MCP default.
        "timeout": 600,
        "enabled": True,
    }


def _register_in_config_file(config_path: Path, tqm_path: str) -> bool:
    """Insert ``mcp_servers.tqmemory`` into one config.yaml. Returns True if changed.

    Raw YAML round-trip (read with ``yaml.safe_load``, write with
    ``atomic_yaml_write``) preserves ``${ENV}`` reference templates because the
    raw file is never expanded. Idempotent: skips when an entry already exists.
    """
    import yaml
    from utils import atomic_yaml_write

    existed = config_path.exists()
    data: dict = {}
    if existed:
        try:
            loaded = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("tqmemory: cannot parse %s (%s) — skipping", config_path, exc)
            return False
        if loaded is None:
            data = {}
        elif isinstance(loaded, dict):
            data = loaded
        else:
            logger.warning("tqmemory: %s is not a mapping — skipping", config_path)
            return False

    servers = data.get("mcp_servers")
    if not isinstance(servers, dict):
        servers = {}

    existing = servers.get(SERVER_NAME)
    if isinstance(existing, dict):
        # Already registered. Leave a user-disabled entry (enabled: false)
        # untouched so we respect intent. Otherwise repair anything that drifted:
        # a stale absolute command path, a missing migrate-on-startup env, a
        # missing stable project root, or a missing per-server timeout. Repairing
        # the project root on EXISTING installs (not just fresh ones) is what lets
        # `hermes update` heal client installs whose memory fragmented by cwd.
        if existing.get("enabled") is False:
            return False
        canonical = _build_entry(tqm_path)
        env = existing.get("env")
        already_correct = (
            existing.get("command") == tqm_path
            and existing.get("args") == ["serve"]
            and isinstance(env, dict)
            and env.get("TQMEMORY_MIGRATE_ON_STARTUP") == "1"
            and env.get("TQMEMORY_PROJECT_ROOT") == canonical["env"]["TQMEMORY_PROJECT_ROOT"]
            and existing.get("timeout") == canonical["timeout"]
        )
        if already_correct:
            return False
        existing["command"] = tqm_path
        existing["args"] = ["serve"]
        if not isinstance(env, dict):
            env = {}
        env.setdefault("TQMEMORY_MIGRATE_ON_STARTUP", "1")
        # Backfill a stable project root so project_id no longer tracks cwd.
        # setdefault: never clobber an operator-chosen TQMEMORY_PROJECT_ROOT.
        env.setdefault("TQMEMORY_PROJECT_ROOT", canonical["env"]["TQMEMORY_PROJECT_ROOT"])
        existing["env"] = env
        existing.setdefault("timeout", canonical["timeout"])
        existing["enabled"] = True
    else:
        servers[SERVER_NAME] = _build_entry(tqm_path)

    # When creating a brand-new config file (a fresh, non-clone profile has no
    # config.yaml), stamp the current schema version so doctor/desktop don't flag
    # the just-created profile as "v0 → N outdated" (a versionless file reads as
    # legacy v0). Existing configs keep their own version — real schema upgrades
    # are the migration pipeline's job, not ours.
    if not existed and "_config_version" not in data:
        try:
            from hermes_cli.config import DEFAULT_CONFIG

            data["_config_version"] = DEFAULT_CONFIG.get("_config_version", 1)
        except Exception:
            pass

    data["mcp_servers"] = servers
    try:
        config_path.parent.mkdir(parents=True, exist_ok=True)
        atomic_yaml_write(config_path, data)
        try:
            os.chmod(config_path, 0o600)
        except OSError:
            pass
    except Exception as exc:
        logger.warning("tqmemory: failed writing %s (%s)", config_path, exc)
        return False
    return True


def _all_profile_config_paths() -> List[Path]:
    """Every profile's config.yaml (default + named), for all-session coverage."""
    paths: List[Path] = []
    try:
        from hermes_cli.profiles import list_profiles

        for prof in list_profiles():
            paths.append(Path(prof.path) / "config.yaml")
    except Exception as exc:
        logger.debug("tqmemory: list_profiles failed (%s) — falling back", exc)

    if not paths:
        # Fallback: the active config only.
        try:
            from hermes_cli.config import get_config_path

            paths.append(Path(get_config_path()))
        except Exception:
            paths.append(Path.home() / ".hermes" / "config.yaml")
    return paths


def ensure_tqmemory_registered(
    tqm_path: str, *, all_profiles: bool = True, quiet: bool = False
) -> int:
    """Register tqmemory into profile config(s). Returns number of files changed."""
    if _is_managed():
        _emit(quiet, "ℹ️  Managed install — leaving MCP config to the system manager.")
        return 0

    targets = _all_profile_config_paths() if all_profiles else []
    if not all_profiles or not targets:
        try:
            from hermes_cli.config import get_config_path

            targets = [Path(get_config_path())]
        except Exception:
            targets = [Path.home() / ".hermes" / "config.yaml"]

    changed = 0
    for cfg in targets:
        if _register_in_config_file(cfg, tqm_path):
            changed += 1
            logger.info("tqmemory: registered in %s", cfg)
    return changed


def register_for_profile(config_path) -> bool:
    """Register tqmemory into ONE profile's config.yaml (register-only, no install).

    Used at profile-creation time so a brand-new profile gets the memory wired up
    immediately, without waiting for the next ``hermes update`` reconcile. Cloned
    profiles already inherit the entry from the source config; this also covers
    fresh (non-clone) profiles. Fast (no network), best-effort, opt-out aware:
    returns ``False`` on opt-out, a managed install, a missing binary, or any
    error — and NEVER raises, so it cannot break profile creation.
    """
    try:
        if opted_out() or _is_managed():
            return False
        tqm_path = resolve_binary()
        if not tqm_path:
            return False
        return _register_in_config_file(Path(config_path), tqm_path)
    except Exception:
        logger.debug("tqmemory: register_for_profile failed", exc_info=True)
        return False


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------

def verify_tqmemory(tqm_path: Optional[str] = None) -> bool:
    """Cheap liveness probe: the binary launches and responds to ``--help``."""
    path = tqm_path or resolve_binary()
    if not path:
        return False
    try:
        res = subprocess.run(
            [path, "--help"],
            capture_output=True, text=True, timeout=_VERIFY_TIMEOUT,
        )
        return res.returncode == 0
    except Exception as exc:
        logger.debug("tqmemory: verify failed (%s)", exc)
        return False


# ---------------------------------------------------------------------------
# Orchestrator — used by setup-hermes.sh (__main__) and `hermes update`
# ---------------------------------------------------------------------------

# Set once per process after a successful reconcile so a belt-and-suspenders
# second call (e.g. the update wrapper's safety net for pip/zip paths) becomes a
# cheap no-op instead of re-running ``uv tool upgrade`` over the network.
_reconciled_this_process = False


def reconcile_tqmemory(quiet: bool = False, once: bool = False) -> Tuple[bool, int]:
    """Install if missing + register across all profiles + verify.

    Returns ``(installed, profiles_changed)``. Always non-fatal — any failure
    is logged/printed, never raised, so it cannot break an install or update.
    When ``once=True`` and a reconcile already succeeded in this process, returns
    immediately without re-running (used by the update wrapper's safety net).
    """
    global _reconciled_this_process
    if once and _reconciled_this_process:
        return (True, 0)
    if opted_out():
        _emit(quiet, "ℹ️  Turbo-Quant Memory disabled (HERMES_NO_TQMEMORY / "
                     "memory.tqmemory_autoinstall=false).")
        return (False, 0)
    if _is_managed():
        return (False, 0)

    try:
        tqm_path = ensure_turbo_memory_installed(quiet=quiet)
        if not tqm_path:
            return (False, 0)
        changed = ensure_tqmemory_registered(tqm_path, all_profiles=True, quiet=quiet)
        alive = verify_tqmemory(tqm_path)
        _reconciled_this_process = True
        if changed:
            _emit(quiet, f"🧠 Turbo-Quant Memory registered in {changed} profile(s)"
                         f"{' and verified' if alive else ''} "
                         f"(opt out: {OPT_OUT_ENV}=1). Takes effect next session.")
        elif alive:
            _emit(quiet, "🧠 Turbo-Quant Memory already configured.")
        return (True, changed)
    except Exception as exc:  # defensive: must never break update/install
        logger.warning("tqmemory reconcile failed: %s", exc, exc_info=True)
        _emit(quiet, f"⚠️  Turbo-Quant Memory setup hit an error ({exc}); continuing.")
        return (False, 0)


def _run(cmd: List[str], timeout: int) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except Exception as exc:
        logger.debug("tqmemory: command %s failed (%s)", cmd[:2], exc)
        return subprocess.CompletedProcess(cmd, 1, "", str(exc))


def _emit(quiet: bool, message: str) -> None:
    if quiet:
        logger.info(message)
    else:
        print(message)


def main(argv: Optional[List[str]] = None) -> int:
    """``python -m hermes_cli.tqmemory_setup`` — used by setup-hermes.sh."""
    logging.basicConfig(level=logging.WARNING, format="%(message)s")
    reconcile_tqmemory(quiet=False)
    return 0  # always succeed: tqmemory is optional and must not fail install


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
