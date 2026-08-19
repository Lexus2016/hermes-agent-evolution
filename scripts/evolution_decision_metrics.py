#!/usr/bin/env python3
"""Decision-level regression metrics from trajectory data (#2917, slice 1 of #2899).

Reads evolution trajectory data and emits per-run **tool-call rate** and
**safety-refusal rate** — the measurement half of #2899's quantization
margin-shrinkage ask (the admission gate is deferred until a routing/cost
layer exists, #2317). Consumes the ``model_calls`` field #2877 wires onto
the live trajectory seam (decisions: ``tool_call`` / ``refusal`` /
``content``); both the evolution store (``TrajectoryLog`` JSONL) and the
agent store (``trajectory_samples.jsonl``) shapes are accepted. Missing
``model_calls`` is handled leniently — skip the run, never crash — so this
is safe to land before #2877. Deterministic: pure functions + thin CLI.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List

__all__ = ["compute_run_metrics", "scan_trajectories", "main"]


def _default_trajectory_dir() -> Path:
    env = os.environ.get("EVOLUTION_PROFILE_DIR", "").strip()
    if env:
        return Path(env) / "trajectories"
    hh = os.environ.get("HERMES_HOME", "").strip()
    return (
        Path(hh) / "evolution" / "trajectories"
        if hh
        else Path.home() / ".hermes" / "evolution" / "trajectories"
    )


def compute_run_metrics(model_calls: Any) -> Dict[str, Any]:
    """Per-run tool-call rate and safety-refusal rate from ``model_calls``.

    Returns ``{}`` when the run carries no usable ``model_calls`` (missing,
    malformed, empty, or all-unknown decisions) — \"not measured\", never a
    zero-failure run.
    """
    if not isinstance(model_calls, list) or not model_calls:
        return {}
    decisions = len(model_calls)
    tool_call_decisions = 0
    tool_calls = 0
    refusals = 0
    content = 0
    for mc in model_calls:
        if not isinstance(mc, dict):
            continue
        decision = mc.get("decision")
        if decision == "tool_call":
            tool_call_decisions += 1
            tool_calls += int(mc.get("tool_call_count", 0) or 0)
        elif decision == "refusal":
            refusals += 1
        elif decision == "content":
            content += 1
    measured = tool_call_decisions + refusals + content
    if measured == 0:
        return {}
    return {
        "decisions": decisions,
        "tool_calls": tool_calls,
        "refusals": refusals,
        "content": content,
        "tool_call_rate": tool_calls / decisions,
        "safety_refusal_rate": refusals / decisions,
        "measured": measured,
    }


def _model_calls_from_entry(entry: Dict[str, Any]) -> Any:
    if isinstance(entry, dict) and "model_calls" in entry:
        return entry["model_calls"]
    return None


def scan_trajectories(trajectory_dir: Path) -> List[Dict[str, Any]]:
    """Scan JSONL trajectory files, one entry per line; skip unreadable or
    unmeasured entries. Returns one dict per measured run."""
    results: List[Dict[str, Any]] = []
    if not trajectory_dir.is_dir():
        return results
    for path in sorted(trajectory_dir.glob("*.jsonl")):
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue
            metrics = compute_run_metrics(_model_calls_from_entry(entry))
            if not metrics:
                continue
            metrics["file"] = path.name
            results.append(metrics)
    return results


def main(argv: List[str]) -> int:
    args = argv[1:]
    trajectory_dir = _default_trajectory_dir()
    if "--dir" in args and args.index("--dir") + 1 < len(args):
        trajectory_dir = Path(args[args.index("--dir") + 1])
    results = scan_trajectories(trajectory_dir)
    print(f"decision metrics for {trajectory_dir} ({len(results)} runs):")
    for r in results:
        print(
            f"  {r['file']}: tool-call {r['tool_call_rate']:.2f} "
            f"({r['tool_calls']}/{r['decisions']}), "
            f"refusal {r['safety_refusal_rate']:.2f} ({r['refusals']}/{r['decisions']})"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
