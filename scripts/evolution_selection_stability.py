#!/usr/bin/env python3
"""Shuffle-order selection stability for the evolution funnel (#3337).

Research (arXiv:2608.18066) shows self-improving pipelines evaluate a single
fixed task ordering, so selection can be an ordering artifact. This module
runs a selection function over N deterministic shuffles of the candidate
backlog, measures pairwise selection agreement (Jaccard), and flags cycles
whose selection flips across orderings as UNSTABLE.

Deterministic and dependency-free: the LLM-in-the-loop selection is injected
as a callable (``select_fn(ids) -> list[id]``) or, via the CLI, as an external
command that reads the shuffled backlog as JSON on stdin and writes the
selected ids as JSON on stdout. Same seed -> same orders -> reproducible.

Used as the variance report for the nightly funnel; integration note in
issue #3337.
"""

from __future__ import annotations

import argparse
import json
import random
import subprocess
import sys
from dataclasses import dataclass, field
from itertools import combinations
from typing import Any, Callable, Dict, List, Sequence

__all__ = [
    "StabilityReport",
    "jaccard",
    "main",
    "make_command_select_fn",
    "selection_stability",
]

DEFAULT_RUNS = 5
DEFAULT_STABILITY_THRESHOLD = 0.6


def jaccard(a: set, b: set) -> float:
    """Jaccard similarity of two sets; 1.0 when both empty."""
    if not a and not b:
        return 1.0
    union = a | b
    return len(a & b) / len(union) if union else 1.0


@dataclass
class StabilityReport:
    """Selection-stability verdict across shuffle orders (#3337).

    Attributes:
        orders: The shuffle seeds/orders actually evaluated.
        selections: Selected ids per order index.
        stability_score: Mean pairwise Jaccard agreement (0.0–1.0).
        min_pairwise: Lowest pairwise Jaccard across orders.
        stable: True when ``stability_score >= threshold``.
        threshold: The stability threshold applied.
        flipped_ids: Ids selected in some orders but dropped in others.
    """

    orders: list[list[Any]] = field(default_factory=list)
    selections: list[list[Any]] = field(default_factory=list)
    stability_score: float = 1.0
    min_pairwise: float = 1.0
    stable: bool = True
    threshold: float = DEFAULT_STABILITY_THRESHOLD
    flipped_ids: list[Any] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "n_orders": len(self.orders),
            "orders": self.orders,
            "selections": self.selections,
            "stability_score": round(self.stability_score, 4),
            "min_pairwise": round(self.min_pairwise, 4),
            "stable": self.stable,
            "threshold": self.threshold,
            "flipped_ids": self.flipped_ids,
        }


def selection_stability(
    backlog: Sequence[Any],
    select_fn: Callable[[list[Any]], list[Any]],
    *,
    runs: int = DEFAULT_RUNS,
    seed: int = 0,
    threshold: float = DEFAULT_STABILITY_THRESHOLD,
) -> StabilityReport:
    """Evaluate ``select_fn`` over ``runs`` shuffled orderings of ``backlog``.

    The SAME underlying candidate set is presented in a different deterministic
    order each run; id sets (not orderings) are compared. ``backlog`` items may
    be plain ids or dicts — an ``id``/``number``/``key`` field is extracted when
    present so shuffled dicts still compare correctly.
    """
    if runs < 2:
        raise ValueError("selection_stability needs at least 2 runs")

    def _key(item: Any) -> Any:
        if isinstance(item, dict):
            for field_name in ("id", "number", "key"):
                if field_name in item:
                    return item[field_name]
        return item

    ids = [_key(item) for item in backlog]
    items_by_id = {_key(item): item for item in backlog}

    report = StabilityReport(threshold=threshold)
    selections: list[set] = []
    for i in range(runs):
        rng = random.Random(seed + i)
        order = ids[:]
        rng.shuffle(order)
        report.orders.append(order)
        selected = [_key(x) for x in select_fn([items_by_id[j] for j in order])]
        selections.append(set(selected))
        report.selections.append(selected)

    pairs = list(combinations(range(runs), 2))
    scores = [jaccard(selections[a], selections[b]) for a, b in pairs]
    report.stability_score = sum(scores) / len(scores)
    report.min_pairwise = min(scores)
    report.stable = report.stability_score >= threshold

    selected_everywhere = set.intersection(*selections)
    selected_anywhere = set.union(*selections)
    report.flipped_ids = sorted(selected_anywhere - selected_everywhere, key=str)
    return report


def make_command_select_fn(
    command: str,
) -> Callable[[list[Any]], list[Any]]:
    """Wrap an external selector command into a ``select_fn``.

    The command receives the shuffled backlog as a JSON array on stdin and
    must write the selected items (or their ids) as a JSON array on stdout.
    A non-zero exit or unparseable output raises ``RuntimeError`` so a broken
    selector is never mistaken for an empty (stable) selection.
    """

    def _select(items: list[Any]) -> list[Any]:
        proc = subprocess.run(
            command,
            shell=True,
            input=json.dumps(items).encode(),
            capture_output=True,
            timeout=600,
            check=False,
        )
        if proc.returncode != 0:
            raise RuntimeError(
                f"selector failed ({proc.returncode}): "
                f"{proc.stderr.decode(errors='replace')[:500]}"
            )
        return json.loads(proc.stdout.decode())

    return _select


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Shuffle-order selection stability check (#3337)."
    )
    parser.add_argument(
        "--backlog",
        required=True,
        help="Path to JSON file containing the backlog (array of ids or objects).",
    )
    parser.add_argument(
        "--select-cmd",
        help=(
            "External selector command (reads JSON array on stdin, writes "
            "selected ids as JSON array on stdout). Omit for a no-op passthrough."
        ),
    )
    parser.add_argument("--runs", type=int, default=DEFAULT_RUNS)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--threshold", type=float, default=DEFAULT_STABILITY_THRESHOLD)
    parser.add_argument("--output", help="Optional path for the JSON report.")
    args = parser.parse_args(argv)

    with open(args.backlog, encoding="utf-8-sig") as fh:
        backlog = json.load(fh)
    if not isinstance(backlog, list):
        print("backlog file must contain a JSON array", file=sys.stderr)
        return 2

    if args.select_cmd:
        select_fn: Callable[[list[Any]], list[Any]] = make_command_select_fn(
            args.select_cmd
        )
    else:
        select_fn = lambda items: [
            (i.get("number", i.get("id")) if isinstance(i, dict) else i) for i in items
        ]

    report = selection_stability(
        backlog,
        select_fn,
        runs=args.runs,
        seed=args.seed,
        threshold=args.threshold,
    )
    payload = json.dumps(report.to_dict(), indent=2)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as fh:
            fh.write(payload + "\n")
    print(payload)
    return 0 if report.stable else 1


if __name__ == "__main__":
    sys.exit(main())
