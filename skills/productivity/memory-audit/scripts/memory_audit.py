#!/usr/bin/env python3
"""Memory quality audit for Hermes.

Checks the built-in memory store (MEMORY.md / USER.md) and an optional
Obsidian vault for health problems that accumulate silently:

  * Secrets leaked into memory (API keys, tokens, passwords).
  * Memory usage approaching the configured char limit.
  * Stale entries (merged PR numbers, old commit SHAs, completed phases).
  * Broken Obsidian wikilinks (`[[Note Name]]` → non-existent file).
  * Missing daily note for the current day.

The script is SILENT when everything is healthy (exit 0, no output) so a
daily cron job produces no spam. When problems are found it prints a
human-readable report and exits non-zero so the cron delivery surfaces it.

Usage:
    python memory_audit.py [--memory-dir DIR] [--vault-path DIR]
    python memory_audit.py --memory-dir ~/.hermes/memories

Exit codes: 0 = healthy, 1 = problems found, 2 = usage error.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import os
import re
import sys
from pathlib import Path
from typing import List, Optional, Tuple

# ── Defaults ────────────────────────────────────────────────────────────
# These mirror hermes_cli/config.py DEFAULT_CONFIG["memory"] but the audit
# is a standalone script (it runs via cron, not inside the agent), so it
# reads no config. The caller can override via CLI flags.  If you changed
# your memory limits in config.yaml, pass them explicitly.
DEFAULT_MEMORY_CHAR_LIMIT = 8000
DEFAULT_USER_CHAR_LIMIT = 1375

# ── Secret detection ───────────────────────────────────────────────────
# Catches common API-key / token / password assignments embedded in memory
# text. Aligned with tools/threat_patterns.py "hardcoded_secret" but
# broadened to also catch bare bearer-token-style values and env-var refs.
_SECRET_PATTERNS = [
    # api_key="sk-...", token: "gho_...", password = "secret123"
    re.compile(
        r"(?:api[_-]?key|token|secret|password|passwd|credential)"
        r"\s*[:=]\s*['\"]?[A-Za-z0-9+/=_-]{20,}",
        re.IGNORECASE,
    ),
    # Bearer <long token>  (Authorization headers pasted into notes)
    re.compile(r"Bearer\s+[A-Za-z0-9+/=_-]{20,}", re.IGNORECASE),
    # Known service prefixes leaked bare (no key= prefix)
    re.compile(r"\b(?:sk-|gho_|ghp_|xox[bpo]-|AKIA[0-9A-Z]{16})[A-Za-z0-9+/=_-]{16,}"),
]

# ── Stale-entry detection ──────────────────────────────────────────────
# Entries that reference transient state which should not persist in
# long-term memory. These are advisory — surfaced for the user to review,
# not auto-removed.
_STALE_PATTERNS = [
    re.compile(r"\bPR\s*#?\d{2,6}\b", re.IGNORECASE),  # PR #123
    re.compile(r"\bfix(?:ed|es)?\s+bug\s+\S+", re.IGNORECASE),  # fixed bug X
    re.compile(r"\bphase\s+\d+\s+done\b", re.IGNORECASE),  # Phase 3 done
    re.compile(r"\b[0-9a-f]{7,40}\b", re.IGNORECASE),  # commit SHA
]


def _read_entries(path: Path) -> Tuple[str, List[str]]:
    """Return (raw_text, entries) from a §-delimited memory file."""
    if not path.exists():
        return "", []
    raw = path.read_text(encoding="utf-8", errors="replace")
    # Split on the § delimiter used by tools/memory_tool.py
    parts = [p.strip() for p in raw.split("\n§\n") if p.strip()]
    # Also split on bare § at line start (older format)
    if not parts:
        parts = [
            p.strip() for p in re.split(r"^§\s*", raw, flags=re.MULTILINE) if p.strip()
        ]
    return raw, parts


def _scan_secrets(entries: List[str]) -> List[str]:
    """Return list of secret matches found in entries."""
    hits: List[str] = []
    for entry in entries:
        for pat in _SECRET_PATTERNS:
            m = pat.search(entry)
            if m:
                # Truncate the match so we don't echo a full secret back
                snippet = m.group(0)[:40] + "..."
                hits.append(snippet)
    return hits


def _scan_stale(entries: List[str]) -> List[str]:
    """Return list of stale-entry snippets found."""
    hits: List[str] = []
    for entry in entries:
        for pat in _STALE_PATTERNS:
            m = pat.search(entry)
            if m:
                snippet = entry[:60].replace("\n", " ") + "..."
                hits.append(snippet)
                break  # one stale signal per entry is enough
    return hits


def _check_usage(raw: str, limit: int, label: str) -> Optional[str]:
    """Return a warning string if usage exceeds 85% of limit, else None."""
    if limit <= 0:
        return None
    used = len(raw)
    pct = (used / limit) * 100
    if pct >= 85:
        return f"{label}: {used}/{limit} chars ({pct:.0f}%) — near limit"
    return None


def audit_hermes_memory(
    memory_dir: Path,
    mem_limit: int = DEFAULT_MEMORY_CHAR_LIMIT,
    user_limit: int = DEFAULT_USER_CHAR_LIMIT,
) -> List[str]:
    """Audit MEMORY.md and USER.md. Return list of problem strings (empty = healthy)."""
    findings: List[str] = []

    for filename, limit in (("MEMORY.md", mem_limit), ("USER.md", user_limit)):
        path = memory_dir / filename
        raw, entries = _read_entries(path)
        if not entries:
            continue  # empty file is healthy

        # Secrets — CRITICAL
        secrets = _scan_secrets(entries)
        for s in secrets:
            findings.append(f"CRITICAL [{filename}] secret detected: {s}")

        # Stale entries — advisory
        for s in _scan_stale(entries):
            findings.append(f"STALE   [{filename}] {s}")

        # Usage
        warn = _check_usage(raw, limit, filename)
        if warn:
            findings.append(f"USAGE   {warn}")

    return findings


def _find_wikilinks(text: str) -> List[str]:
    """Extract [[Note Name]] targets from markdown text."""
    # [[Note]] or [[Note|Alias]] or [[folder/Note#heading]]
    return re.findall(r"\[\[([^\]|#]+)(?:[|#][^\]]*)?\]\]", text)


def audit_obsidian_vault(vault_path: Path) -> List[str]:
    """Audit an Obsidian vault for broken wikilinks and missing daily note."""
    findings: List[str] = []
    if not vault_path or not vault_path.exists():
        return findings  # no vault configured — not an error

    # Build a set of all note names (without extension) for quick lookup
    note_names: dict[str, Path] = {}
    for md in vault_path.rglob("*.md"):
        note_names[md.stem.lower()] = md
        note_names[md.name.lower()] = md

    # Broken wikilinks
    broken: List[str] = []
    for md in vault_path.rglob("*.md"):
        try:
            text = md.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for target in _find_wikilinks(text):
            target_clean = target.strip().split("/")[-1]  # folder/Note → Note
            if target_clean.lower() not in note_names:
                broken.append(f"{md.name}: [[{target}]] → not found")

    for b in broken:
        findings.append(f"WIKILINK [{b}]")

    # Daily note freshness — check if today's daily note exists.
    # Common conventions: YYYY-MM-DD.md or YYYY/MM/DD.md
    today = _dt.date.today()
    candidates = [
        vault_path / f"{today.isoformat()}.md",
        vault_path / str(today.year) / str(today.month) / f"{today.isoformat()}.md",
        vault_path / "Daily" / f"{today.isoformat()}.md",
    ]
    if not any(p.exists() for p in candidates):
        findings.append(f"DAILY   missing daily note for {today.isoformat()}")

    return findings


def run_audit(
    memory_dir: Optional[Path] = None,
    vault_path: Optional[Path] = None,
    mem_limit: int = DEFAULT_MEMORY_CHAR_LIMIT,
    user_limit: int = DEFAULT_USER_CHAR_LIMIT,
) -> List[str]:
    """Run all configured checks. Return combined findings list."""
    findings: List[str] = []

    if memory_dir:
        findings.extend(audit_hermes_memory(memory_dir, mem_limit, user_limit))

    if vault_path:
        findings.extend(audit_obsidian_vault(vault_path))

    return findings


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Hermes memory quality audit")
    parser.add_argument(
        "--memory-dir",
        type=Path,
        default=None,
        help="Path to the Hermes memories directory (contains MEMORY.md / USER.md)",
    )
    parser.add_argument(
        "--vault-path",
        type=Path,
        default=None,
        help="Path to an Obsidian vault to check for broken wikilinks",
    )
    parser.add_argument(
        "--memory-char-limit",
        type=int,
        default=DEFAULT_MEMORY_CHAR_LIMIT,
        help=f"Char limit for MEMORY.md (default {DEFAULT_MEMORY_CHAR_LIMIT})",
    )
    parser.add_argument(
        "--user-char-limit",
        type=int,
        default=DEFAULT_USER_CHAR_LIMIT,
        help=f"Char limit for USER.md (default {DEFAULT_USER_CHAR_LIMIT})",
    )
    args = parser.parse_args(argv)

    if args.memory_dir is None and args.vault_path is None:
        # Default to the standard Hermes memories location
        home = os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes"))
        args.memory_dir = Path(home) / "memories"

    findings = run_audit(
        memory_dir=args.memory_dir,
        vault_path=args.vault_path,
        mem_limit=args.memory_char_limit,
        user_limit=args.user_char_limit,
    )

    if not findings:
        return 0  # SILENT — healthy

    print("⚠ Memory audit found issues:\n")
    for f in findings:
        if f.startswith("CRITICAL"):
            print(f"  🚨 {f}")
        elif f.startswith("STALE"):
            print(f"  📅 {f}")
        elif f.startswith("USAGE"):
            print(f"  📊 {f}")
        elif f.startswith("WIKILINK"):
            print(f"  🔗 {f}")
        elif f.startswith("DAILY"):
            print(f"  📝 {f}")
        else:
            print(f"  • {f}")
    print(
        f"\n{len(findings)} issue(s) found. Review with /memory or edit the files directly."
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
