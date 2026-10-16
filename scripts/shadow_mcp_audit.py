#!/usr/bin/env python3
"""Audit the shadow-MCP outbound endpoint contact log (#90).

Reads the append-only JSONL contact log written by
``evolution.lib.shadow_mcp`` (default ``~/.hermes/logs/shadow_mcp.jsonl``) and
prints a per-endpoint digest: owning MCP server, endpoint, host, contact count,
first/last-seen, and the most recent verdict (``allow``/``alert``/``deny``).

This is a **read-only audit**: it never mutates config or the log, and prints
only endpoint metadata. Endpoints in the log are already redacted (no query
strings / userinfo), so the CLI never surfaces secrets. Exit code 0 = no
unapproved contacts, 1 = one or more ``alert``/``deny`` contacts, so it can
gate a scheduled audit.

Usage::

    python3 scripts/shadow_mcp_audit.py [--log PATH] [--json]
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def _default_log() -> Path:
    try:
        from hermes_constants import get_hermes_home  # noqa: PLC0415

        return get_hermes_home() / "logs" / "shadow_mcp.jsonl"
    except Exception:
        return Path.home() / ".hermes" / "logs" / "shadow_mcp.jsonl"


def read_contacts(log_path: Path) -> list[dict[str, Any]]:
    """Read the JSONL contact log and return the final aggregate per endpoint.

    The log is append-only with one line per contact (each carrying the
    cumulative count at that time); we keep the last line for each
    ``(server, endpoint)`` key so the digest reports final state.
    """
    if not log_path.exists():
        return []
    latest: dict[tuple[str, str], dict[str, Any]] = {}
    for line in log_path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        key = (str(rec.get("server", "")), str(rec.get("endpoint", "")))
        latest[key] = rec
    contacts = list(latest.values())
    contacts.sort(key=lambda c: str(c.get("last_seen", "")), reverse=True)
    return contacts


def _print_human(contacts: list[dict[str, Any]], source: str) -> None:
    print(f"shadow-mcp-audit: {source} — {len(contacts)} endpoint(s) contacted")
    unapproved = [
        c for c in contacts if c.get("last_verdict") in ("alert", "deny")
    ]
    if not contacts:
        print("no outbound endpoint contacts recorded")
        return
    for c in contacts:
        flag = "  " if c.get("last_verdict") == "allow" else "!!"
        print(
            f"{flag} {c.get('last_verdict', 'allow'):5}  "
            f"{c.get('host', '?'):40}  x{c.get('count', 0):<4}  "
            f"server={c.get('server', '?')}  endpoint={c.get('endpoint', '?')}"
        )
    print(
        f"{len(unapproved)} unapproved contact(s) of {len(contacts)}. "
        "Tighten via the `shadow_mcp.allow`/`shadow_mcp.deny` config."
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Audit the shadow-MCP outbound endpoint contact log."
    )
    parser.add_argument(
        "--log",
        help="Path to the shadow-mcp JSONL contact log (default: ~/.hermes/logs/shadow_mcp.jsonl).",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit a single JSON document instead of human-readable lines.",
    )
    args = parser.parse_args(argv)

    log_path = Path(args.log).expanduser() if args.log else _default_log()
    contacts = read_contacts(log_path)
    unapproved = [
        c for c in contacts if c.get("last_verdict") in ("alert", "deny")
    ]

    if args.json:
        print(
            json.dumps(
                {
                    "source": str(log_path),
                    "contacts": contacts,
                    "unapproved": unapproved,
                },
                indent=2,
            )
        )
    else:
        _print_human(contacts, str(log_path))

    return 1 if unapproved else 0


if __name__ == "__main__":
    raise SystemExit(main())
