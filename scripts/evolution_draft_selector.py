#!/usr/bin/env python3
"""Parallel draft mode (#798): build N draft tasks, select best, cost-aware routing.

Increment 1 (PR #817) added ``build_draft_tasks`` and ``select_best_draft`` but
left them without real call sites and deferred cost-aware routing.  This module
now also provides ``route_cost_tier(complexity)`` — a deterministic mapping from
task complexity to a model tier hint — and is wired into
``evolution_orchestrator.py`` as real call sites (no more dead code).
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

DEFAULT_MAX_DRAFTERS = 3
_OK = frozenset({"completed", "success", "ok"})
_PAT = r"(?:^|\n)\s*(?:#{1,3}\s|```|[-*]\s|\d+\.\s)"

# ── Anti-conformity pressure (#2761) ─────────────────────────────────────────
# A draft population has "converged" when the consensus winner has a near-twin
# (another OK draft whose summary is >= ANTICONFORMITY_SIMILARITY similar).
# Selection then keeps the highest-scoring CONTRARIAN (distinct) OK draft alive
# instead of the consensus one, so what the next stage receives still contains
# candidate diversity (conformity law).
ANTICONFORMITY_SIMILARITY = 0.8

# ── Cost-aware routing (#798 inc 2) ──────────────────────────────────────────
# Map complexity bucket -> (tier label, config hint for delegation.model).
# The hint is a *suggestion*: the orchestrator passes it to delegate_task's
# model override or writes it to ``delegation.model`` in config.yaml.  The
# actual model name is resolved by the runtime's provider layer.
_TIERS: Dict[str, Tuple[str, str]] = {
    "trivial": ("cheap", "fast-cheap"),
    "simple": ("cheap", "fast-cheap"),
    "moderate": ("standard", "standard"),
    "complex": ("frontier", "frontier"),
    "unknown": ("standard", "standard"),
}

# Keyword → complexity bucket.  Scanned in order; first match wins so that
# more-specific keywords (``refactor``) are checked before generic ones.
_CMAP: List[Tuple[str, str]] = [
    ("trivial", "trivial"),
    ("fix typo", "trivial"),
    ("lint", "trivial"),
    ("format", "trivial"),
    ("rename", "trivial"),
    ("simple", "simple"),
    ("doc", "simple"),
    ("comment", "simple"),
    ("test", "simple"),
    ("stub", "simple"),
    ("moderate", "moderate"),
    ("add", "moderate"),
    ("extend", "moderate"),
    ("refactor", "moderate"),
    ("wire", "moderate"),
    ("integrate", "moderate"),
    ("complex", "complex"),
    ("architect", "complex"),
    ("design", "complex"),
    ("security", "complex"),
    ("multi-agent", "complex"),
    ("protocol", "complex"),
    ("migration", "complex"),
]


def route_cost_tier(complexity: str) -> Dict[str, str]:
    """Map a task complexity description to a cost-tier model hint.

    ``complexity`` is free-text (the task goal or a complexity label).  The
    function scans for keywords, picks the first matching complexity bucket,
    and returns ``{"complexity": <bucket>, "tier": <tier>, "model": <hint>}``.

    Unknown / empty input falls back to the ``"standard"`` tier so the caller
    never gets an un-actionable result.
    """
    text = (complexity or "").lower().strip()
    bucket = "unknown"
    for keyword, label in _CMAP:
        if keyword in text:
            bucket = label
            break
    tier, model_hint = _TIERS.get(bucket, _TIERS["unknown"])
    return {"complexity": bucket, "tier": tier, "model": model_hint}


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


def _summary_similarity(a: str, b: str) -> float:
    """Word-set Jaccard similarity between two summaries (0.0–1.0)."""
    if not a or not b:
        return 0.0
    wa = set(re.findall(r"[a-z0-9]+", a.lower()))
    wb = set(re.findall(r"[a-z0-9]+", b.lower()))
    if not wa or not wb:
        return 0.0
    return len(wa & wb) / len(wa | wb)


def _distinct_variants(drafts: List[Dict[str, Any]]) -> Tuple[int, float]:
    """Pick the winner with anti-conformity pressure applied (#2761).

    Baseline is the best-scoring OK draft. When the population has converged
    — the baseline has a near-twin (>= ANTICONFORMITY_SIMILARITY similar) —
    and a genuinely distinct OK variant exists, the highest-scoring distinct
    variant wins instead. Every OK draft gets a ``contrarian`` flag (distinct
    from the consensus) so the orchestrator's output shows which variant the
    pressure kept alive. Returns (best_index, best_score).
    """
    ok = [d for d in drafts if d["ok"]]
    if not ok:
        return -1, 0.0
    best = max(ok, key=lambda d: d["score"])
    for d in ok:
        d["contrarian"] = (
            _summary_similarity(d["summary"], best["summary"])
            < ANTICONFORMITY_SIMILARITY
        )
    if not any(d["index"] != best["index"] and not d["contrarian"] for d in ok):
        return best["index"], best["score"]
    contrarians = [d for d in ok if d["contrarian"]]
    if not contrarians:
        return best["index"], best["score"]
    alt = max(contrarians, key=lambda d: d["score"])
    return alt["index"], alt["score"]


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
    bi, bs = _distinct_variants(drafts)
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
        print(
            "usage: evolution_draft_selector.py {build,select,route} ...",
            file=sys.stderr,
        )
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
    if cmd == "route":
        complexity = _flag(args, "--complexity")
        if not complexity:
            # Allow bare positional argument: route "fix typo in README"
            positional = [a for a in args if not a.startswith("-")]
            complexity = positional[0] if positional else ""
        if not complexity:
            print(
                'usage: evolution_draft_selector.py route --complexity "task desc"',
                file=sys.stderr,
            )
            return 2
        print(json.dumps(route_cost_tier(complexity), ensure_ascii=False))
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
