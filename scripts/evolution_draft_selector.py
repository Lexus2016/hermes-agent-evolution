#!/usr/bin/env python3
"""Parallel draft mode (#798 inc 1): build N draft tasks, select best. Cost routing deferred."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

DEFAULT_MAX_DRAFTERS = 3
_OK = frozenset({"completed", "success", "ok"})
_PAT = r"(?:^|\n)\s*(?:#{1,3}\s|```|[-*]\s|\d+\.\s)"


def _s(v: Any) -> str:
    return v.strip() if isinstance(v, str) else ""


def build_draft_tasks(
    goal: str,
    n_drafters: int = DEFAULT_MAX_DRAFTERS,
    *,
    context: str = "",
    toolsets: Optional[List[str]] = None,
) -> Tuple[List[Dict[str, Any]], int]:
    """Build N identical leaf-worker tasks for parallel draft mode via delegate_task."""
    n = max(1, int(n_drafters))
    ctx = (
        _s(context)
        or "You are one of several independent drafters. Produce your best complete draft."
    )
    t = {
        "goal": _s(goal),
        "context": ctx,
        "toolsets": list(toolsets) if toolsets is not None else ["web", "file"],
        "role": "leaf",
    }
    return [dict(t) for _ in range(n)], 0


def _score(text: str) -> float:
    if not text:
        return 0.0
    s = min(len(text) / 2000.0, 1.0) * 0.3
    s += min(len(re.findall(_PAT, text)) / 10.0, 1.0) * 0.3
    s += min(len(re.findall(r"https?://\S+|\[\d+\]", text)) / 5.0, 1.0) * 0.4
    return round(s, 4)


def select_best_draft(delegate_output: Any) -> Tuple[int, float, List[Dict[str, Any]]]:
    """Score drafts, pick winner: (best_index, best_score, drafts)."""
    results = (
        delegate_output.get("results", [])
        if isinstance(delegate_output, dict)
        else (delegate_output if isinstance(delegate_output, list) else [])
    )
    drafts: List[Dict[str, Any]] = []
    for pos, entry in enumerate(results if isinstance(results, list) else []):
        if not isinstance(entry, dict):
            entry = {}
        try:
            idx = int(entry.get("task_index", pos))
        except (TypeError, ValueError):
            idx = pos
        st, sm = _s(entry.get("status")).lower(), _s(entry.get("summary"))
        ok = st in _OK and bool(sm)
        drafts.append({
            "index": idx,
            "status": st,
            "ok": ok,
            "summary": sm,
            "score": _score(sm) if ok else 0.0,
        })
    drafts.sort(key=lambda d: d["index"])
    bi, bs = -1, 0.0
    for d in drafts:
        if d["ok"] and d["score"] > bs:
            bs, bi = d["score"], d["index"]
    return bi, bs, drafts


def _load_json(path: Optional[str]) -> Tuple[Any, Optional[str]]:
    try:
        raw = Path(path).read_text(encoding="utf-8") if path else sys.stdin.read()
        return json.loads(raw), None
    except (OSError, ValueError) as exc:
        return None, str(exc)


def _flag(args: List[str], name: str) -> Optional[str]:
    if name in args:
        i = args.index(name)
        if i + 1 < len(args):
            return args[i + 1]
    return None


def main(argv: List[str]) -> int:
    if len(argv) < 2 or argv[1] in ("-h", "--help"):
        print("usage: evolution_draft_selector.py {build,select} ...", file=sys.stderr)
        return 2
    cmd, args = argv[1], argv[2:]
    if cmd == "build":
        goal = _flag(args, "--goal")
        if not goal:
            return 2
        n = DEFAULT_MAX_DRAFTERS
        dval = _flag(args, "--drafters")
        if dval:
            try:
                n = int(dval)
            except ValueError:
                return 2
        ts_str = _flag(args, "--toolsets")
        ts = [t.strip() for t in ts_str.split(",") if t.strip()] if ts_str else None
        tasks, dropped = build_draft_tasks(
            goal, n, context=_flag(args, "--context") or "", toolsets=ts
        )
        print(json.dumps({"tasks": tasks, "dropped": dropped}, ensure_ascii=False))
        return 0
    if cmd == "select":
        path = args[0] if args and not args[0].startswith("-") else None
        data, err = _load_json(path)
        if err:
            return 2
        bi, bs, drafts = select_best_draft(data)
        print(
            json.dumps(
                {"best_index": bi, "best_score": bs, "drafts": drafts},
                ensure_ascii=False,
            )
        )
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
