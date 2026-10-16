#!/usr/bin/env python3
"""Shared retrieval/failure memo for parallel subagents (#65).

Parallel evolution subagents redundantly fetch the same pages (research
workers each scrape the same arXiv/repo URLs) and re-attempt known-failing
edits every cycle (parallel draft workers all try the same change that
already failed). A shared, file-backed memo cuts that redundancy: one
worker's fetch summary becomes the next worker's skip, and a recorded
failure stops the next re-attempt.

This module is the deterministic primitive (pure functions + thin CLI, the
``scripts/evolution_*.py`` family convention). It is consumed by real call
sites in:

* ``scripts/evolution_orchestrator.py`` — the research fan-out: ``build``
  consults the memo BEFORE building worker prompts, embeds already-summarized
  URLs into every worker's context so the worker skips the fetch, and the
  ``memo`` subcommand gives the skill a direct consult/record/failing CLI.
* ``scripts/evolution_draft_selector.py`` — the parallel draft/edit fan-out:
  ``build_draft_tasks`` consults the memo and skips the whole fan-out when
  the goal is a known-failing edit (``dropped == n_drafters``).

Memo file format (JSON, evolution-dir convention — ``$EVOLUTION_PROFILE_DIR``,
else ``<hermes_home>/evolution``, else ``~/.hermes/evolution``):

    {
      "version": 1,
      "updated_at": "2026-08-19T09:00:00Z",
      "retrievals": {"https://arxiv.org/abs/2608.10424": "one-line summary"},
      "failures": {"<goal-or-issue-key>": "already tried — why it failed"}
    }

CLI:

    python scripts/evolution_shared_memo.py lookup <url> [--memo PATH]
    python scripts/evolution_shared_memo.py record <url> <summary...> [--memo PATH]
    python scripts/evolution_shared_memo.py failing <key> <note...> [--memo PATH]
    python scripts/evolution_shared_memo.py status [--memo PATH]

Exit codes: 0 on success, 2 on bad input.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

_MEMO_VERSION = 1
# A memo summary is ONE LINE — cap the recorded content so the file stays a
# compact consult table, not a dump store.
_MAX_SUMMARY_CHARS = 500


def default_memo_path() -> Path:
    """Resolve the shared memo path WITHOUT hardcoding a profile name.

    Priority (matches the evolution script family + the runtime):
      1. ``$EVOLUTION_PROFILE_DIR`` — set explicitly by the evolution cron.
      2. ``<hermes_home>/evolution`` — ``$HERMES_HOME`` or ``~/.hermes``.
    """
    env = os.environ.get("EVOLUTION_PROFILE_DIR", "").strip()
    if env:
        return Path(env) / "shared-memo.json"
    hermes_home = Path(
        os.environ.get("HERMES_HOME", "").strip() or (Path.home() / ".hermes")
    )
    return hermes_home / "evolution" / "shared-memo.json"


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class SharedMemo:
    """File-backed shared memo: URL -> summary, and key -> failure note."""

    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = Path(path) if path is not None else default_memo_path()
        self.retrievals: dict[str, str] = {}
        self.failures: dict[str, str] = {}
        self._load()

    # ── persistence ────────────────────────────────────────────────────────
    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            return  # missing/corrupt memo = empty memo, never a crash
        if not isinstance(data, dict):
            return
        retrievals = data.get("retrievals")
        failures = data.get("failures")
        if isinstance(retrievals, dict):
            self.retrievals = {str(k): str(v) for k, v in retrievals.items() if str(v)}
        if isinstance(failures, dict):
            self.failures = {str(k): str(v) for k, v in failures.items() if str(v)}

    def save(self) -> None:
        """Persist the memo to its JSON file (creates the directory)."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": _MEMO_VERSION,
            "updated_at": _now(),
            "retrievals": self.retrievals,
            "failures": self.failures,
        }
        self.path.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
        )

    # ── retrieval memo (URL -> one-line summary) ───────────────────────────
    def lookup_summary(self, url: str) -> Optional[str]:
        """The recorded summary for ``url``, or None if never fetched."""
        return self.retrievals.get(url)

    def record_summary(self, url: str, summary: str) -> bool:
        """Record a one-line summary for ``url``. Returns True when NEW.

        An existing entry is kept (first writer wins — later workers skip the
        fetch entirely), unless the new summary is the only content we have.
        """
        url = (url or "").strip()
        summary = (summary or "").strip()
        if not url or not summary:
            return False
        if url in self.retrievals:
            return False
        self.retrievals[url] = summary[:_MAX_SUMMARY_CHARS]
        return True

    # ── failure memo (key -> already-tried note) ───────────────────────────
    def is_known_failing(self, key: str) -> bool:
        """True when ``key`` was already tried and recorded as failing."""
        return (key or "").strip() in self.failures

    def record_failure(self, key: str, note: str) -> bool:
        """Record an already-tried failure note for ``key``. Returns True when NEW."""
        key = (key or "").strip()
        note = (note or "").strip()
        if not key or not note:
            return False
        if key in self.failures:
            return False
        self.failures[key] = note[:_MAX_SUMMARY_CHARS]
        return True

    # ── introspection ──────────────────────────────────────────────────────
    def stats(self) -> dict[str, Any]:
        """Memo health summary (counts + path) for CLI/status output."""
        return {
            "path": str(self.path),
            "retrieval_entries": len(self.retrievals),
            "failure_entries": len(self.failures),
            "updated_at": _now(),
        }


def load_memo(path: Optional[Path] = None) -> SharedMemo:
    """Load the shared memo (missing/corrupt file -> empty memo)."""
    return SharedMemo(path)


def consult_before_fetch(
    memo: SharedMemo, url: str, fetch_fn: Callable[[str], str]
) -> tuple[str, str]:
    """Consult the memo BEFORE fetching ``url``.

    Returns ``(source, content)`` where ``source`` is ``"memo"`` (the fetch
    was SKIPPED — the caller uses the recorded summary) or ``"fetch"`` (the
    memo missed, ``fetch_fn(url)`` ran, and the result was recorded for the
    next worker). ``fetch_fn`` is only ever called on a memo miss — this is
    the exact contract parallel workers rely on to cut duplicate fetches.
    """
    existing = memo.lookup_summary(url)
    if existing is not None:
        return "memo", existing
    content = fetch_fn(url)
    summary = content if isinstance(content, str) else str(content)
    memo.record_summary(url, summary)
    memo.save()
    return "fetch", summary


# ── CLI ──────────────────────────────────────────────────────────────────────
def _flag(args: list[str], name: str) -> Optional[str]:
    if name in args:
        i = args.index(name)
        if i + 1 < len(args):
            return args[i + 1]
    return None


def _strip_flag(args: list[str], name: str) -> tuple[list[str], Optional[str]]:
    """Remove ``name <value>`` from ``args``, returning (remaining, value).

    Unlike :func:`_flag`, the flag AND its value are consumed so positional
    arguments never absorb the memo path (e.g. a multi-word summary followed
    by ``--memo PATH``).
    """
    remaining: list[str] = []
    value: Optional[str] = None
    i = 0
    while i < len(args):
        if args[i] == name and i + 1 < len(args):
            value = args[i + 1]
            i += 2
            continue
        remaining.append(args[i])
        i += 1
    return remaining, value


def _cmd_lookup(args: list[str]) -> int:
    args, memo_path = _strip_flag(args, "--memo")
    positional = [a for a in args if not a.startswith("-")]
    if not positional:
        print(
            "usage: evolution_shared_memo.py lookup <url> [--memo PATH]",
            file=sys.stderr,
        )
        return 2
    memo = load_memo(Path(memo_path) if memo_path else None)
    summary = memo.lookup_summary(positional[0])
    if summary is None:
        print(json.dumps({"url": positional[0], "source": "miss", "summary": None}))
        return 0
    print(
        json.dumps(
            {"url": positional[0], "source": "memo", "summary": summary},
            ensure_ascii=False,
        )
    )
    return 0


def _cmd_record(args: list[str]) -> int:
    args, memo_path = _strip_flag(args, "--memo")
    positional = [a for a in args if not a.startswith("-")]
    if len(positional) < 2:
        print(
            "usage: evolution_shared_memo.py record <url> <summary...> [--memo PATH]",
            file=sys.stderr,
        )
        return 2
    memo = load_memo(Path(memo_path) if memo_path else None)
    url, summary = positional[0], " ".join(positional[1:])
    new = memo.record_summary(url, summary)
    memo.save()
    print(
        json.dumps(
            {"url": url, "recorded": new, "source": "memo" if not new else "fetch"},
            ensure_ascii=False,
        )
    )
    return 0


def _cmd_failing(args: list[str]) -> int:
    args, memo_path = _strip_flag(args, "--memo")
    positional = [a for a in args if not a.startswith("-")]
    if len(positional) < 2:
        print(
            "usage: evolution_shared_memo.py failing <key> <note...> [--memo PATH]",
            file=sys.stderr,
        )
        return 2
    memo = load_memo(Path(memo_path) if memo_path else None)
    key, note = positional[0], " ".join(positional[1:])
    new = memo.record_failure(key, note)
    memo.save()
    print(json.dumps({"key": key, "recorded": new}, ensure_ascii=False))
    return 0


def _cmd_status(args: list[str]) -> int:
    memo_path = _flag(args, "--memo")
    memo = load_memo(Path(memo_path) if memo_path else None)
    print(json.dumps(memo.stats(), ensure_ascii=False))
    return 0


def main(argv: list[str]) -> int:
    if len(argv) < 2 or argv[1] in ("-h", "--help"):
        print(
            "usage: evolution_shared_memo.py {lookup,record,failing,status} ...",
            file=sys.stderr,
        )
        return 2
    cmd, args = argv[1], argv[2:]
    if cmd == "lookup":
        return _cmd_lookup(args)
    if cmd == "record":
        return _cmd_record(args)
    if cmd == "failing":
        return _cmd_failing(args)
    if cmd == "status":
        return _cmd_status(args)
    print(f"unknown command: {cmd}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
