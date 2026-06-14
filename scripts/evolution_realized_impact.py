#!/usr/bin/env python3
"""Realized-impact feedback — did a MERGED change actually help, or was it blind?

The funnel/metrics measure the pipeline up to the merge (proposed -> merged) and
its calibration (selection_efficiency = merged/selected). They do NOT answer the
question that closes the loop: *of what we merged, what actually delivered real
value?* Without that, evolution is blind — the agent optimizes a PREDICTED impact
it never checks against reality, and can keep shipping plausible-but-useless code.

This is the measurement spine for that loop. It is a LIGHT, deterministic
aggregator: the agent (integration + introspection skills) writes a ledger; this
script reads it and reports the realized-impact rate + flags. It does no judging
itself (no creds, no LLM) — the verdicts come from the agent verifying real
sessions; this turns those verdicts into an evidence signal the pipeline acts on.

Ledger: ``<evolution_dir>/realized/ledger.jsonl`` — one JSON object per line.
  * at merge (integration):   {"issue", "merged_at": "YYYY-MM-DD",
                               "predicted_impact": <0..1>, "target": "<one line>"}
  * at verification later (introspection):
                               {"issue", "verdict": "confirmed|no-signal|regressed",
                               "verified_at": "YYYY-MM-DD", "note": "<one line>"}
Lines with the same ``issue`` are folded (latest verdict wins; merge metadata
kept). A change is "matured" once ``maturity_days`` have passed since merge — only
then is the absence of a verdict a problem (the verification step didn't run).

Pure functions + explicit IO so it is import-safe and unit-testable.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

VERDICTS_GOOD = {"confirmed"}
VERDICTS_BAD = {"no-signal", "regressed"}


def load_ledger(ledger_file: Path) -> List[Dict[str, Any]]:
    """Read the realized-impact ledger (one JSON object per line), oldest-first.

    Folds multiple lines for the same ``issue`` into one record: merge metadata
    from the first sighting, latest verdict/verified_at/note from the last.
    Malformed lines are skipped (the ledger must never crash the pipeline).
    """
    if not ledger_file.exists():
        return []
    folded: Dict[Any, Dict[str, Any]] = {}
    order: List[Any] = []
    for ln in ledger_file.read_text(encoding="utf-8").splitlines():
        ln = ln.strip()
        if not ln:
            continue
        try:
            rec = json.loads(ln)
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(rec, dict) or "issue" not in rec:
            continue
        key = rec["issue"]
        if key not in folded:
            folded[key] = {}
            order.append(key)
        # Later lines override earlier keys for this issue (verdict supersedes).
        for k, v in rec.items():
            if v is not None:
                folded[key][k] = v
    return [folded[k] for k in order]


def _days_between(a: Optional[str], b: Optional[str]) -> Optional[int]:
    """Whole days from ISO date ``a`` to ISO date ``b`` (both YYYY-MM-DD)."""
    if not a or not b:
        return None
    try:
        from datetime import date

        ya, ma, da = (int(x) for x in str(a)[:10].split("-"))
        yb, mb, db = (int(x) for x in str(b)[:10].split("-"))
        return (date(yb, mb, db) - date(ya, ma, da)).days
    except (ValueError, TypeError):
        return None


def compute_realized(
    records: List[Dict[str, Any]],
    today: str,
    last: int = 30,
    maturity_days: int = 5,
    streak_k: int = 3,
) -> Dict[str, Any]:
    """Aggregate the ledger into a realized-impact signal.

    ``today`` is passed in (never read the clock here — keeps it deterministic
    and testable, same discipline as the rest of the evolution scripts).
    """
    window = records[-last:] if last and last > 0 else list(records)

    verdicted = [r for r in window if r.get("verdict") in (VERDICTS_GOOD | VERDICTS_BAD)]
    confirmed = [r for r in verdicted if r.get("verdict") in VERDICTS_GOOD]

    # Matured-but-unverified: merged long enough ago to have been exercised, yet
    # the verification step never recorded a verdict — the loop isn't closing.
    matured_unverified = [
        r
        for r in window
        if r.get("verdict") not in (VERDICTS_GOOD | VERDICTS_BAD)
        and (_days_between(r.get("merged_at"), today) or 0) >= maturity_days
    ]

    realized_rate = (len(confirmed) / len(verdicted)) if verdicted else None

    # Consecutive-miss streak over the most recent verdicted changes (by order).
    streak = 0
    for r in reversed(verdicted):
        if r.get("verdict") in VERDICTS_BAD:
            streak += 1
        else:
            break

    flags: List[str] = []
    if len(verdicted) >= streak_k and streak >= streak_k:
        flags.append(
            f"REALIZED_IMPACT_LOW: last {streak} merged changes delivered no real "
            "value — shift to consolidation/refactor and require owner sign-off "
            "before new features"
        )
    if len(verdicted) >= 3 and realized_rate is not None and realized_rate < 0.5:
        flags.append(
            "REALIZED_RATE_LOW: <50% of verified merges actually helped — predicted "
            "impact is over-optimistic; raise the bar and recalibrate"
        )
    if len(matured_unverified) >= streak_k:
        flags.append(
            f"UNVERIFIED_BACKLOG: {len(matured_unverified)} matured merges never "
            "verified — the post-merge verification step is not running"
        )

    return {
        "merged_tracked": len(window),
        "verified": len(verdicted),
        "confirmed": len(confirmed),
        "matured_unverified": len(matured_unverified),
        "realized_impact_rate": round(realized_rate, 3) if realized_rate is not None else None,
        "miss_streak": streak,
        "flags": flags,
    }


def _pct(x: Optional[float]) -> str:
    return "n/a" if x is None else f"{x:.0%}"


def format_realized(h: Dict[str, Any]) -> str:
    tail = " | ".join(h["flags"]) if h["flags"] else "healthy"
    return (
        f"[evolution-realized-impact] tracked={h['merged_tracked']} "
        f"verified={h['verified']} confirmed={h['confirmed']} "
        f"realized_rate={_pct(h['realized_impact_rate'])} "
        f"miss_streak={h['miss_streak']} unverified_matured={h['matured_unverified']} | {tail}"
    )


def main(argv: List[str]) -> int:
    import os

    evolution_dir = Path(
        os.environ.get(
            "EVOLUTION_PROFILE_DIR",
            str(Path.home() / ".hermes" / "profiles" / "user1" / "evolution"),
        )
    )
    args = argv[1:]
    last = 30
    if "--last" in args:
        i = args.index("--last")
        if i + 1 < len(args):
            try:
                last = int(args[i + 1])
            except ValueError:
                last = 30
    today = None
    if "--today" in args:
        i = args.index("--today")
        if i + 1 < len(args):
            today = args[i + 1]
    if not today:
        # The only clock read, and only in the CLI entrypoint (not the pure core).
        from datetime import date, timezone, datetime

        today = datetime.now(timezone.utc).date().isoformat()

    records = load_ledger(evolution_dir / "realized" / "ledger.jsonl")
    health = compute_realized(records, today=today, last=last)
    line = format_realized(health)
    print(line)

    if "--summary" in args:
        sidecar = evolution_dir / "realized-impact.txt"
        try:
            sidecar.parent.mkdir(parents=True, exist_ok=True)
            sidecar.write_text(line + "\n", encoding="utf-8")
        except OSError:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
