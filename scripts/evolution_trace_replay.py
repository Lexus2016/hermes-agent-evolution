#!/usr/bin/env python3
"""CLI for deterministic session-trace replay, resume and branch (issue #85).

``agent/tool_call_capture.py`` records what the agent actually did, but until
now a recorded trajectory was a dead file: nothing could replay it, resume an
interrupted session from its first pending call, or branch from an arbitrary
recorded point. That is exactly what Moirai's trace-optimization needs — and
what the DeepSeek harness-style session recording pattern (the issue's
namesake) provides.

This CLI is the real call site that keeps the replay machinery from being dead
code: it loads a recorded trajectory (either the single-object ``.json`` from
:meth:`TrajectoryLog.save` or the per-turn ``.jsonl`` from
:meth:`TrajectoryLog.append`), replays it deterministically, reports the
resume point, or writes a branch prefix.

Usage::

    evolution_trace_replay.py replay <path> [--from N] [--to N] [--json]
    evolution_trace_replay.py resume <path>
    evolution_trace_replay.py branch <path> --to N [--out DIR]

``replay`` prints a per-step action sequence + the task-level ``outcome``
(success / failed / incomplete / empty). ``resume`` prints the index of the
first step worth resuming from (exit 1 when the run is already clean).
``branch`` writes a new trajectory containing steps ``[0 .. --to]`` and prints
the output path. All output is deterministic — no LLM, no network.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

# Make both scripts/ (trajectory logger) and agent/ (capture) importable.
ROOT = Path(__file__).resolve().parent.parent
for p in (str(ROOT / "scripts"), str(ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)

from agent.tool_call_capture import branch_trajectory, replay_trajectory, resume_index
from evolution_trajectory_logger import iter_trajectories

EXIT_OK = 0
EXIT_BAD_INPUT = 2
EXIT_CLEAN_RUN = 1  # resume: nothing to resume from


def _load_single(path: Path) -> Optional[Any]:
    """Load the (first) trajectory log in a file; None on unreadable input."""
    logs = iter_trajectories(path)
    return logs[0] if logs else None


def _print_json(obj: dict[str, Any]) -> None:
    print(json.dumps(obj, indent=2, sort_keys=True))


def cmd_replay(argv: list[str]) -> int:
    path = argv[0]
    start_at = 0
    stop_at = None
    as_json = False
    i = 1
    while i < len(argv):
        arg = argv[i]
        if arg == "--from" and i + 1 < len(argv):
            start_at = int(argv[i + 1])
            i += 2
        elif arg == "--to" and i + 1 < len(argv):
            stop_at = int(argv[i + 1])
            i += 2
        elif arg == "--json":
            as_json = True
            i += 1
        else:
            return _err(f"unknown argument: {arg}")
    log = _load_single(Path(path))
    if log is None:
        return _err(f"could not load trajectory: {path}")
    result = replay_trajectory(log, start_at=start_at, stop_at=stop_at)
    result["source"] = path
    result["entries_total"] = len(log.entries)
    if as_json:
        _print_json(result)
        return EXIT_OK
    print(f"=== Replay: {path} ({len(log.entries)} recorded steps) ===")
    print(f"Outcome: {result['outcome'].upper()}")
    for s in result["steps"]:
        dur = f" ({s['duration_ms']}ms)" if s["duration_ms"] is not None else ""
        print(f"  [{s['index']}] {s['tool']}: {s['status']}{dur}")
    return EXIT_OK


def cmd_resume(argv: list[str]) -> int:
    path = argv[0]
    log = _load_single(Path(path))
    if log is None:
        return _err(f"could not load trajectory: {path}")
    idx = resume_index(log)
    if idx is None:
        print(
            f"clean run: no step to resume from ({len(log.entries)} steps all successful)"
        )
        return EXIT_CLEAN_RUN
    print(
        f"resume at index {idx} "
        f"(step {idx}: {log.entries[idx].tool}, status {log.entries[idx].result_status})"
    )
    return EXIT_OK


def cmd_branch(argv: list[str]) -> int:
    path = argv[0]
    up_to = None
    out_dir: Optional[Path] = None
    i = 1
    while i < len(argv):
        arg = argv[i]
        if arg == "--to" and i + 1 < len(argv):
            up_to = int(argv[i + 1])
            i += 2
        elif arg == "--out" and i + 1 < len(argv):
            out_dir = Path(argv[i + 1])
            i += 2
        else:
            return _err(f"unknown argument: {arg}")
    if up_to is None:
        return _err("branch requires --to N")
    log = _load_single(Path(path))
    if log is None:
        return _err(f"could not load trajectory: {path}")
    branch = branch_trajectory(log, up_to)
    if out_dir is None:
        out_dir = Path(path).parent
    out_dir.mkdir(parents=True, exist_ok=True)
    name = Path(path).stem
    out_path = out_dir / f"{name}.branch-{up_to}.json"
    out_path.write_text(branch.to_json(), encoding="utf-8")
    print(out_path)
    return EXIT_OK


def _err(msg: str) -> int:
    print(f"[evolution-trace-replay] {msg}", file=sys.stderr)
    return EXIT_BAD_INPUT


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(
            "usage: evolution_trace_replay.py <replay|resume|branch> <path> [opts]",
            file=sys.stderr,
        )
        return EXIT_BAD_INPUT
    sub, rest = argv[1], argv[2:]
    if sub == "replay":
        if not rest:
            return _err("replay requires a path")
        return cmd_replay(rest)
    if sub == "resume":
        if not rest:
            return _err("resume requires a path")
        return cmd_resume(rest)
    if sub == "branch":
        if not rest:
            return _err("branch requires a path")
        return cmd_branch(rest)
    return _err(f"unknown subcommand: {sub}")


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
