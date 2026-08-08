#!/usr/bin/env python3
"""Floor-test gate for BenchJack defense (#1267, Slice 3).

Wires null-agent floor scores into the merge gate. A metric that a null-agent
(which solves nothing) can pass is NOT trusted — if the PR's metrics fall at or
below the floor, the merge is blocked. Design: call from merge gate or standalone.
Usage: python3 scripts/evolution_floor_gate.py --scores scores.jsonl
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

DEFAULT_FLOOR_SCORES: Dict[str, float] = {"mean_total": 0.0, "mean_tool_score": 0.0}
FLOOR_MARGIN = 0.05  # PR must score >5% above floor to be trusted


@dataclass
class FloorTestResult:
    blocked: bool
    violations: List[str]
    floor_scores: Dict[str, float]
    pr_scores: Dict[str, float]


def load_floor_scores(scores_path: Optional[str] = None) -> Dict[str, float]:
    """Load null-agent floor scores from a JSONL file or return defaults."""
    if not scores_path:
        return dict(DEFAULT_FLOOR_SCORES)
    path = Path(scores_path)
    if not path.exists():
        return dict(DEFAULT_FLOOR_SCORES)
    try:
        rows = [
            json.loads(l)
            for l in path.read_text(encoding="utf-8").splitlines()
            if l.strip()
        ]
    except (OSError, ValueError, json.JSONDecodeError):
        return dict(DEFAULT_FLOOR_SCORES)
    scores: Dict[str, float] = {}
    for row in rows:
        for key in ("mean_total", "mean_tool_score", "total", "tool_score"):
            if key in row and isinstance(row[key], (int, float)):
                scores.setdefault(key, float(row[key]))
    if "mean_total" not in scores:
        totals = [
            float(r["total"]) for r in rows if isinstance(r.get("total"), (int, float))
        ]
        if totals:
            scores["mean_total"] = sum(totals) / len(totals)
    return scores


def check_floor_gate(pr_scores, floor_scores=None, floor_margin=FLOOR_MARGIN):
    """Block if any PR metric is at or below floor * (1 + margin).

    A metric at floor level means the null agent (solves nothing) could achieve
    it — the metric measures compliance, not capability (#1267).
    """
    floors = floor_scores or DEFAULT_FLOOR_SCORES
    violations: List[str] = []
    for metric, floor in floors.items():
        pr_value = pr_scores.get(metric)
        if pr_value is None:
            continue
        threshold = floor * (1.0 + floor_margin)
        if pr_value <= threshold:
            violations.append(
                f"FLOOR_TEST_BLOCK: '{metric}' = {pr_value:.4f} ≤ null-agent floor "
                f"({floor:.4f} × {1 + floor_margin:.0%} = {threshold:.4f}) — "
                f"metric not trustworthy (#1267)"
            )
    return FloorTestResult(bool(violations), violations, dict(floors), dict(pr_scores))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--scores", default=None, help="null-agent floor scores JSONL")
    ap.add_argument("--pr-scores", default=None, help="PR scores JSON")
    ap.add_argument("--margin", type=float, default=FLOOR_MARGIN)
    args = ap.parse_args(argv)
    pr_scores: Dict[str, float] = {}
    if args.pr_scores:
        try:
            pr_scores = json.loads(args.pr_scores)
        except ValueError:
            pass
    result = check_floor_gate(pr_scores, load_floor_scores(args.scores), args.margin)
    print(
        json.dumps(
            {
                "blocked": result.blocked,
                "floor_scores": result.floor_scores,
                "pr_scores": result.pr_scores,
                "violations": result.violations,
            },
            indent=2,
        )
    )
    return 1 if result.blocked else 0


if __name__ == "__main__":
    raise SystemExit(main())
