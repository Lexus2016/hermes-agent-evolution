#!/usr/bin/env python3
"""PR reflection signal — GEPA-style feedback loop for pipeline self-improvement.

Issue #1584: add a reflect-and-rewrite pass driven by PR review feedback
(merge/reject signal). This script mines recently closed PRs (merged AND
rejected) from the evolution repo, extracts actionable feedback patterns, and
writes a one-line sidecar (``pr-reflection.txt``) that the analysis stage reads
to calibrate its next selection.

GEPA principle (arXiv:2507.19457): read execution trace + metric's natural-
language feedback, reflect on failures, keep a Pareto frontier so no
improvement is lost. This is the SMALLEST coherent increment — NOT the full
multi-optimizer, just the feedback-collection + reflection sidecar.

Runs as a ``no_agent`` cron job (no LLM). Uses ``gh`` CLI for GitHub queries.
Pure functions + explicit paths so it is import-safe and unit-testable.

Сторонній сигнал відгуку PR — GEPA-стиль зворотного зв'язку.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

#: Default lookback window (days) for closed PRs.
DEFAULT_WINDOW_DAYS = 7

#: Minimum PR count before the signal is considered informative.
#: Below this, the sidecar is "insufficient data" rather than a trend.
MIN_PR_FOR_SIGNAL = 2


def _evolution_dir() -> Path:
    return Path(
        os.environ.get(
            "EVOLUTION_PROFILE_DIR",
            str(Path.home() / ".hermes" / "evolution"),
        )
    )


def query_closed_prs(
    repo: str = "Lexus2016/hermes-agent-evolution",
    days: int = DEFAULT_WINDOW_DAYS,
) -> List[Dict[str, Any]]:
    """Query recently closed PRs via ``gh`` CLI.

    Returns a list of dicts with keys: number, title, state, mergedAt,
    closedAt, headRefName, body, labels. Returns ``[]`` on any error — a
    failed GitHub query must NOT crash the cron job.
    """
    try:
        result = subprocess.run(
            [
                "gh",
                "pr",
                "list",
                "--repo",
                repo,
                "--state",
                "closed",
                "--limit",
                "30",
                "--json",
                "number,title,state,mergedAt,closedAt,headRefName,body,labels",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            logger.warning("[pr-reflection] gh query failed: %s", result.stderr[:200])
            return []
        prs = json.loads(result.stdout)
        # Filter by recency: keep PRs closed within `days` window.
        import time as _time

        cutoff = _time.time() - days * 86400
        from datetime import datetime, timezone

        def _to_ts(dt_str: Optional[str]) -> float:
            if not dt_str:
                return 0.0
            try:
                return datetime.fromisoformat(dt_str.replace("Z", "+00:00")).timestamp()
            except (ValueError, AttributeError):
                return 0.0

        recent = [
            pr
            for pr in prs
            if _to_ts(pr.get("closedAt") or pr.get("mergedAt")) >= cutoff
        ]
        return recent
    except (json.JSONDecodeError, subprocess.TimeoutExpired, FileNotFoundError) as exc:
        logger.warning("[pr-reflection] gh query error: %s", exc)
        return []


def _classify_pr(pr: Dict[str, Any]) -> str:
    """Classify a closed PR as 'merged', 'rejected', or 'closed-other'.

    A PR with ``mergedAt`` set was merged (success signal).
    A PR with evolution branch prefix but no merge was rejected (failure signal).
    """
    if pr.get("mergedAt"):
        return "merged"
    head = (pr.get("headRefName") or "").lower()
    if head.startswith("evolution/") or "evolution" in head:
        return "rejected"
    return "closed-other"


def extract_reject_reason(pr: Dict[str, Any]) -> Optional[str]:
    """Extract the rejection reason from a PR body or title.

    Evolution PRs that were skipped/rejected typically contain keywords like
    'out-of-scope', 'already-exists', 'could not get CI green', 'harmful',
    'rejected'. This extracts the matching keyword for pattern clustering.
    """
    text = ((pr.get("body") or "") + " " + (pr.get("title") or "")).lower()
    reasons = [
        "out-of-scope",
        "already-exists",
        "harmful",
        "could not get ci green",
        "needs-decomposition",
        "duplicate",
        "stale",
    ]
    for reason in reasons:
        if reason in text:
            return reason
    return None


def reflect(prs: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Build a reflection summary from a list of closed PRs.

    Returns a dict with:
    - ``total``: total closed PRs in window
    - ``merged``: count of merged PRs
    - ``rejected``: count of rejected evolution PRs
    - ``reject_reasons``: Counter of reasons
    - ``merge_rate``: merged / total (None if total == 0)
    - ``patterns``: list of actionable reflection strings
    """
    from collections import Counter

    merged = 0
    rejected = 0
    reject_reasons: Counter = Counter()

    for pr in prs:
        cls = _classify_pr(pr)
        if cls == "merged":
            merged += 1
        elif cls == "rejected":
            rejected += 1
            reason = extract_reject_reason(pr)
            if reason:
                reject_reasons[reason] += 1

    total = len(prs)
    merge_rate = merged / total if total > 0 else None

    #: Merge rate below this fraction signals low selection viability.
    LOW_MERGE_RATE_THRESHOLD = 0.4

    patterns: List[str] = []
    if (
        merge_rate is not None
        and merge_rate < LOW_MERGE_RATE_THRESHOLD
        and total >= MIN_PR_FOR_SIGNAL
    ):
        patterns.append(
            f"Low merge rate ({merge_rate:.0%}) — selection may be picking "
            f"low-viability issues. Apply stricter viability re-check before branching."
        )
    for reason, count in reject_reasons.most_common(3):
        if count >= 2:
            patterns.append(
                f"Repeat rejection: '{reason}' ({count}x) — analysis should "
                f"pre-filter issues matching this pattern."
            )
    if not patterns and merge_rate is not None and merge_rate >= 0.7:
        patterns.append(
            "High merge rate — selection is healthy, no calibration needed."
        )
    return {
        "total": total,
        "merged": merged,
        "rejected": rejected,
        "reject_reasons": dict(reject_reasons),
        "merge_rate": round(merge_rate, 3) if merge_rate is not None else None,
        "patterns": patterns,
    }


def format_sidecar(h: Dict[str, Any]) -> str:
    """Format the reflection as a one-line sidecar string.

    Same pattern as ``evolution_realized_impact.format_realized`` — a single
    ``[evolution-pr-reflection]`` line that the analysis skill greps for.
    """
    rate_str = f"{h['merge_rate']:.0%}" if h["merge_rate"] is not None else "n/a"
    reasons_str = (
        ", ".join(f"{k}={v}" for k, v in sorted(h["reject_reasons"].items())) or "none"
    )
    patterns_str = " | ".join(h["patterns"]) if h["patterns"] else "no patterns"
    return (
        f"[evolution-pr-reflection] closed={h['total']} "
        f"merged={h['merged']} rejected={h['rejected']} "
        f"merge_rate={rate_str} reject_reasons={reasons_str} | {patterns_str}"
    )


def run_reflection(
    evolution_dir: Optional[Path] = None,
    days: int = DEFAULT_WINDOW_DAYS,
    prs: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Query closed PRs, reflect, and write the sidecar. Returns the summary.

    When ``prs`` is provided (testing), skip the ``gh`` query and use the
    injected list. When ``evolution_dir`` is None, uses the default path.
    """
    if evolution_dir is None:
        evolution_dir = _evolution_dir()
    if prs is None:
        prs = query_closed_prs(days=days)

    summary = reflect(prs)
    sidecar_text = format_sidecar(summary)
    sidecar_path = evolution_dir / "pr-reflection.txt"

    try:
        evolution_dir.mkdir(parents=True, exist_ok=True)
        sidecar_path.write_text(sidecar_text + "\n", encoding="utf-8")
        summary["sidecar"] = str(sidecar_path)
        summary["sidecar_written"] = True
    except OSError as exc:
        logger.warning("[pr-reflection] failed to write sidecar: %s", exc)
        summary["sidecar_written"] = False

    return summary


def main(argv: Optional[List[str]] = None) -> int:
    """Cron entry point: run reflection, print summary as JSON."""
    days = DEFAULT_WINDOW_DAYS
    if argv:
        for arg in argv[1:]:
            if arg.startswith("--days="):
                try:
                    days = int(arg.split("=", 1)[1])
                except ValueError:
                    pass
    summary = run_reflection(days=days)
    print(json.dumps(summary, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
