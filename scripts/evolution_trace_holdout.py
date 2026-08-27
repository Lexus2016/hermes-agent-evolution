#!/usr/bin/env python3
"""Self-Harness trace hold-out selector (#3209 slice 1 / #3226).

The Self-Harness loop (arXiv 2606.09498) must not judge a candidate harness
update against the same sessions that produced it — that measures memorization,
not generalization. This script is the deterministic core of the
validation-based update-selection layer: it picks a small, reproducible
**hold-back mini-batch** of recent trace sessions and returns the complement
(the training/analysis set) so downstream stages can score a candidate update
against sessions it never saw.

Session discovery mirrors ``introspection_extract.build_digest`` exactly so the
hold-out set is drawn from the SAME population the miner/proposer analyze:

  * ``*.jsonl`` transcripts — session id is the filename stem.
  * ``request_dump_*.json`` snapshots (#238) — session id is the ``session_id``
    field inside; multiple dumps of one session are deduped keeping the most
    complete snapshot (largest message count), so one session contributes once.

Selection is deterministic: session ids are sorted, then a seeded
``random.Random`` samples the hold-out fraction. Same inputs + same seed ⇒ same
hold-out set, so a regression gate can reproduce the split across cycles.

Pure, typed, import-safe functions + a thin CLI — the same shape as the other
``scripts/evolution_*.py`` helpers.

CLI:

    evolution_trace_holdout.py --window-days=7 --holdout-fraction=0.1 --seed=0

Prints one JSON object ``{"holdout": [...], "train": [...], "meta": {...}}`` to
stdout and exits 0.
"""

from __future__ import annotations

import argparse
import json
import random
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    from introspection_extract import _sessions_dir
except Exception:  # pragma: no cover - keep import path explicit on failure
    raise

DEFAULT_WINDOW_DAYS = 7
DEFAULT_HOLDOUT_FRACTION = 0.1
DEFAULT_SEED = 0


def _fresh(path: Path, cutoff: float) -> bool:
    """True when a session file's mtime falls inside the window."""
    try:
        return path.stat().st_mtime >= cutoff
    except OSError:
        return False


def discover_sessions(
    sessions_dir: Path, window_days: int = DEFAULT_WINDOW_DAYS, now: float | None = None
) -> List[str]:
    """Enumerate recent session ids exactly as ``build_digest`` does.

    Returns a SORTED, de-duplicated list of session ids within the window.
    ``*.jsonl`` ids come from the filename stem; ``request_dump_*.json`` ids
    come from the ``session_id`` field, deduped keeping the most complete
    snapshot. A missing/empty dir yields ``[]`` (never raises)."""
    now = now if now is not None else time.time()
    cutoff = now - window_days * 86400
    ids: Dict[str, int] = {}  # session_id -> message count (for dedup)

    if sessions_dir.is_dir():
        for path in sorted(sessions_dir.glob("*.jsonl")):
            if _fresh(path, cutoff):
                ids.setdefault(path.stem, 0)
        for path in sorted(sessions_dir.glob("request_dump_*.json")):
            if not _fresh(path, cutoff):
                continue
            try:
                with open(path, "r", encoding="utf-8", errors="replace") as fh:
                    obj = json.load(fh)
            except (OSError, ValueError):
                continue
            sid = obj.get("session_id") if isinstance(obj, dict) else None
            if not isinstance(sid, str) or not sid:
                continue
            msgs = (
                obj.get("request", {}).get("messages", [])
                if isinstance(obj, dict)
                else []
            )
            n = len(msgs) if isinstance(msgs, list) else 0
            # Keep the most complete snapshot of a session.
            if n >= ids.get(sid, 0):
                ids[sid] = n
    return sorted(ids)


def select_holdout(
    session_ids: List[str],
    holdout_fraction: float = DEFAULT_HOLDOUT_FRACTION,
    seed: int = DEFAULT_SEED,
) -> Tuple[List[str], List[str]]:
    """Deterministically split session ids into (holdout, train).

    ``holdout`` is a seeded random sample of ``round(len * fraction)`` ids;
    ``train`` is the complement. Edge cases: a fraction that rounds to 0 yields
    an empty holdout (nothing held back — caller may treat that as 'no
    validation possible'); a population smaller than the rounded count yields
    the whole population as holdout and an empty train. Pure + deterministic:
    same inputs + same seed ⇒ same split."""
    ids = sorted(session_ids)
    n = len(ids)
    k = int(round(n * holdout_fraction))
    if k <= 0:
        return [], ids
    if k >= n:
        return ids, []
    rng = random.Random(seed)
    holdout = rng.sample(ids, k)
    holdout_set = set(holdout)
    train = [i for i in ids if i not in holdout_set]
    return holdout, train


def build_holdout(
    sessions_dir: Path,
    window_days: int = DEFAULT_WINDOW_DAYS,
    holdout_fraction: float = DEFAULT_HOLDOUT_FRACTION,
    seed: int = DEFAULT_SEED,
    now: float | None = None,
) -> Dict[str, Any]:
    """Full pipeline: discover recent sessions, split, return a JSON-ready dict."""
    ids = discover_sessions(sessions_dir, window_days=window_days, now=now)
    holdout, train = select_holdout(ids, holdout_fraction=holdout_fraction, seed=seed)
    return {
        "holdout": holdout,
        "train": train,
        "meta": {
            "window_days": window_days,
            "holdout_fraction": holdout_fraction,
            "seed": seed,
            "total_sessions": len(ids),
            "holdout_count": len(holdout),
            "train_count": len(train),
        },
    }


def _parse_args(argv: List[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Deterministic trace hold-out selection (#3226)"
    )
    p.add_argument("--window-days", type=int, default=DEFAULT_WINDOW_DAYS)
    p.add_argument("--holdout-fraction", type=float, default=DEFAULT_HOLDOUT_FRACTION)
    p.add_argument("--seed", type=int, default=DEFAULT_SEED)
    p.add_argument("--sessions-dir", type=Path, default=None)
    return p.parse_args(argv[1:])


def main(argv: List[str]) -> int:
    args = _parse_args(argv)
    sessions_dir = args.sessions_dir or _sessions_dir()
    out = build_holdout(
        sessions_dir,
        window_days=args.window_days,
        holdout_fraction=args.holdout_fraction,
        seed=args.seed,
    )
    print(json.dumps(out, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
