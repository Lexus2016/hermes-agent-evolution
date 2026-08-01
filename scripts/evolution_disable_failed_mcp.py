#!/usr/bin/env python3
"""Disable tqmemory MCP server when binary is missing or broken (#1541).

When ``turbo-memory-mcp`` fails every connection (zero successes, 1190+ error
lines per batch), the gateway retries 3× then parks and self-probes every 300s
forever — massive log noise + startup latency. The existing opt-out
(``HERMES_NO_TQMEMORY=1`` / ``memory.tqmemory_autoinstall: false``) requires
manual action; this script automates the disable when the binary is broken.

Usage::

    python scripts/evolution_disable_failed_mcp.py            # check + disable
    python scripts/evolution_disable_failed_mcp.py --dry-run  # report only

Exit 0 = success, 1 = error.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import List, Optional, Tuple


def _import_tqmemory():
    """Import tqmemory_setup from hermes_cli/ or scripts/. Returns module or None."""
    repo_root = Path(__file__).resolve().parent.parent
    for mod_path in [
        str(repo_root / "hermes_cli"),
        str(Path(__file__).resolve().parent),
    ]:
        if mod_path not in sys.path:
            sys.path.insert(0, mod_path)
    for mod_name in ["hermes_cli.tqmemory_setup", "tqmemory_setup"]:
        try:
            return __import__(mod_name, fromlist=["resolve_binary", "verify_tqmemory"])
        except ImportError:
            continue
    return None


def check_tqmemory_health() -> Tuple[bool, str]:
    """Check if tqmemory binary exists and launches. Returns (healthy, message)."""
    mod = _import_tqmemory()
    if mod is None:
        return False, "tqmemory_setup module not found — cannot check"
    binary = mod.resolve_binary()
    if not binary:
        return False, "turbo-memory-mcp binary not found on PATH"
    if not mod.verify_tqmemory(binary):
        return False, f"turbo-memory-mcp at {binary} failed --help probe"
    return True, f"turbo-memory-mcp healthy at {binary}"


def disable_in_profiles(dry_run: bool = False) -> List[str]:
    """Set ``mcp_servers.tqmemory.enabled: false`` in all profile configs."""
    import yaml

    mod = _import_tqmemory()
    if mod is None:
        return []
    changed: List[str] = []
    for cfg_path in mod._all_profile_config_paths():
        if not cfg_path.exists():
            continue
        try:
            data = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        servers = data.get("mcp_servers")
        if not isinstance(servers, dict) or mod.SERVER_NAME not in servers:
            continue
        entry = servers[mod.SERVER_NAME]
        if isinstance(entry, dict) and entry.get("enabled") is False:
            continue
        if dry_run:
            changed.append(str(cfg_path))
            continue
        if isinstance(entry, dict):
            entry["enabled"] = False
        try:
            from utils import atomic_yaml_write

            atomic_yaml_write(cfg_path, data)
            changed.append(str(cfg_path))
        except Exception:
            pass
    return changed


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Disable tqmemory MCP when binary broken (#1541)"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Report without modifying"
    )
    args = parser.parse_args(argv)

    healthy, msg = check_tqmemory_health()
    print(f"[disable-mcp] tqmemory health: {msg}")
    if healthy:
        print("[disable-mcp] binary is healthy — no action needed")
        return 0

    print("[disable-mcp] binary is broken/missing — disabling in all profiles")
    changed = disable_in_profiles(dry_run=args.dry_run)
    action = "would be disabled in" if args.dry_run else "disabled in"
    for path in changed:
        print(f"  {action}: {path}")
    if not changed:
        print("[disable-mcp] no profiles needed changes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
