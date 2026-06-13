#!/usr/bin/env python3
"""Deterministic pre-extract for evolution-introspection (#89).

evolution-introspection previously loaded RAW session transcripts (last 7 days)
into the LLM context — unbounded megabytes, the single largest context bomb in
the pipeline, AND it put the user's private text into the model context.

This script (no LLM) scans the session JSONL files for PROBLEM SIGNALS only and
emits a compact, ANONYMIZED digest — counts per signal/tool, generic shapes,
never raw content. The skill feeds ONLY this digest to the model. Raw private
text never enters the context (complements the PII redaction gate #82).

Signals extracted:
  * tool_failures  — tool results that look like failures, attributed to the
    tool (via tool_call_id -> name from the preceding assistant turn). Reuses
    agent.loop_guard's failure markers for consistency.
  * timeouts       — results mentioning timeout / timed out.
  * refusals       — assistant text expressing "I can't / no access / denied".
  * repeated_tool_runs — same tool called many times consecutively (the spiral
    shape loop_guard guards against), counted per session.

Output: a JSON digest to stdout (and optionally a file), a few KB max.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
try:
    from agent.loop_guard import _looks_like_failure  # pure (re + typing only)
except Exception:  # pragma: no cover - keep standalone if import path differs
    _FALLBACK = ("error:", "failed", "permission denied", "command not found",
                 "no such file", "timed out", "timeout", "traceback (most recent call")

    def _looks_like_failure(content: Any) -> bool:  # type: ignore
        return isinstance(content, str) and any(m in content.lower() for m in _FALLBACK)


_TIMEOUT_RE = re.compile(r"\b(timed out|timeout)\b", re.IGNORECASE)
_REFUSAL_RE = re.compile(
    r"\b(i can('|no)?t|cannot|no access|access denied|not permitted|don'?t have (access|permission))\b",
    re.IGNORECASE,
)
_REPEAT_THRESHOLD = 5  # same tool >=N consecutive in a session is a "repeated run"


def _iter_lines(path: Path):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except ValueError:
                    continue
    except OSError:
        return


def scan_session(path: Path) -> Dict[str, Any]:
    """Return per-session signal counts (no raw text)."""
    tool_failures: Counter = Counter()
    timeouts = 0
    refusals = 0
    id_to_tool: Dict[str, str] = {}
    consec_tool = None
    consec_n = 0
    max_runs: Counter = Counter()  # tool -> max consecutive in this session

    for obj in _iter_lines(path):
        role = obj.get("role")
        if role == "assistant":
            tcs = obj.get("tool_calls") or []
            names = []
            for tc in tcs:
                if isinstance(tc, dict) and tc.get("function"):
                    nm = tc["function"].get("name")
                    if nm:
                        names.append(nm)
                        if tc.get("id"):
                            id_to_tool[tc["id"]] = nm
            # consecutive same-single-tool run tracking
            if len(set(names)) == 1:
                tool = names[0]
                if tool == consec_tool:
                    consec_n += 1
                else:
                    consec_tool, consec_n = tool, 1
                max_runs[consec_tool] = max(max_runs[consec_tool], consec_n)
            else:
                consec_tool, consec_n = None, 0
            content = obj.get("content")
            if isinstance(content, str) and _REFUSAL_RE.search(content):
                refusals += 1
        elif role == "tool":
            content = obj.get("content")
            tool = id_to_tool.get(obj.get("tool_call_id"), "unknown")
            if _looks_like_failure(content):
                tool_failures[tool] += 1
            if isinstance(content, str) and _TIMEOUT_RE.search(content):
                timeouts += 1

    repeated = {t: n for t, n in max_runs.items() if n >= _REPEAT_THRESHOLD}
    return {
        "tool_failures": dict(tool_failures),
        "timeouts": timeouts,
        "refusals": refusals,
        "repeated_tool_runs": repeated,
    }


def build_digest(sessions_dir: Path, window_days: int = 7, now: float | None = None) -> Dict[str, Any]:
    now = now if now is not None else time.time()
    cutoff = now - window_days * 86400
    failures: Counter = Counter()
    timeouts = 0
    refusals = 0
    repeated: Dict[str, Dict[str, int]] = {}  # tool -> {max_consecutive, sessions}
    scanned = 0

    files = sorted(sessions_dir.glob("*.jsonl")) if sessions_dir.is_dir() else []
    for path in files:
        try:
            if path.stat().st_mtime < cutoff:
                continue
        except OSError:
            continue
        scanned += 1
        s = scan_session(path)
        failures.update(s["tool_failures"])
        timeouts += s["timeouts"]
        refusals += s["refusals"]
        for tool, n in s["repeated_tool_runs"].items():
            r = repeated.setdefault(tool, {"max_consecutive": 0, "sessions": 0})
            r["max_consecutive"] = max(r["max_consecutive"], n)
            r["sessions"] += 1

    return {
        "window_days": window_days,
        "sessions_scanned": scanned,
        "signals": {
            "tool_failures": dict(failures.most_common()),
            "timeouts": timeouts,
            "refusals_or_access_denied": refusals,
            "repeated_tool_runs": repeated,
        },
    }


def _sessions_dir() -> Path:
    return Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes"))) / "sessions"


def main(argv: List[str]) -> int:
    days = 7
    for a in argv[1:]:
        if a.startswith("--days="):
            try:
                days = int(a.split("=", 1)[1])
            except ValueError:
                pass
    digest = build_digest(_sessions_dir(), window_days=days)
    print(json.dumps(digest, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
