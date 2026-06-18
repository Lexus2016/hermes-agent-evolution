#!/usr/bin/env python3
"""Orchestrator-workers fan-out for the research evaluator-optimizer loop (#300).

The orchestrator-workers + evaluator-optimizer workflow (Anthropic, "Building
Effective Agents") is: an ORCHESTRATOR decomposes a research sub-task into N
independent worker prompts, fans them out to N WORKERS via ``delegate_task``,
collects the worker candidate outputs, then an EVALUATOR scores them and an
OPTIMIZER decides whether to ACCEPT or run another pass.

This module owns the deterministic FAN-OUT and COLLECTION halves of that loop —
the part that is repeatable shell-callable logic, not LLM reasoning:

  * ``build_worker_tasks`` — turn one sub-task + a list of decomposition angles
    into the exact ``tasks=[...]`` batch payload ``delegate_task`` expects (one
    leaf worker per angle, each a self-contained prompt), capped at the user's
    ``delegation.max_concurrent_children`` so the fan-out never exceeds what the
    runtime will actually run in parallel.
  * ``collect_candidates`` — parse the JSON ``delegate_task`` returns
    (``{"results": [{task_index, status, summary, ...}]}``) back into ordered
    candidate objects keyed to their originating angle, in the exact shape
    ``scripts/evolution_evaluator.py`` consumes (a list of ``{"scores": {}, ...}``
    so the evaluator can score and pick the best).

The LLM still does the open-ended work (write the angles, run the workers, judge
the candidates). This module is the small deterministic glue between the
orchestrator's decompose step and the evaluator's score step. Pure functions +
a thin CLI mirror the sibling ``scripts/evolution_*.py`` helpers so it is
import-safe and unit-testable, and the CLI gives the skill's terminal toolset a
real call site (no dead code).

SCOPE BOUNDARY: this is the fan-out + collection slice ONLY. The iterate-until-
quality optimizer LOOP (re-running workers on OPTIMIZE, bounded termination) is
the sibling issue #301 and is deliberately NOT built here — ``collect_candidates``
emits candidates straight into the existing ``evolution_evaluator.decide`` gate,
which is where #301 will wire the loop.

CLI (the skill calls these from its terminal toolset):

    # 1. decompose: emit the delegate_task batch payload for a sub-task
    evolution_orchestrator.py build \
        --subtask "How do top agents bound delegation depth?" \
        --angle "official docs / source" --angle "failure modes" --angle "benchmarks"

    # 2. collect: turn delegate_task's JSON output into evaluator candidates
    delegate_results.json | evolution_orchestrator.py collect --angles angles.json

``build`` prints ``{"tasks": [...], "dropped": int}`` and exits 0 (2 on bad
input). ``collect`` prints ``{"candidates": [...], "ok": int, "failed": int}``
and exits 0 (2 on bad input). The candidates payload is accepted as-is by
``evolution_evaluator.py`` (it reads ``{"candidates": [...]}``).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Mirror the runtime default in tools/delegate_tool.py
# (delegation.max_concurrent_children, default 3). The orchestrator never fans
# out wider than the runtime will run in parallel — extra angles past the cap
# are dropped (and reported) rather than silently queued or over-spawned.
DEFAULT_MAX_WORKERS = 3

# Leaf workers do focused research synthesis: web + file reading, no further
# delegation. Matches the toolsets evolution-research grants its own subagents.
DEFAULT_WORKER_TOOLSETS: Tuple[str, ...] = ("web", "file")

# delegate_task statuses that mean the worker produced a usable result. Anything
# else (timeout, error, interrupted, max_iterations) is a failed candidate that
# must NOT be scored as if it were a real answer.
_OK_STATUSES = frozenset({"completed", "success", "ok"})


def _clean_str(value: Any) -> str:
    """Coerce to a stripped string; non-strings -> ""."""
    return value.strip() if isinstance(value, str) else ""


def build_worker_task(
    subtask: str,
    angle: str,
    *,
    toolsets: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Build ONE leaf-worker task dict for ``delegate_task``'s batch array.

    Each worker is a self-contained prompt: the shared research sub-task plus the
    ONE decomposition angle that worker owns. Subagents have no memory of the
    orchestrator's conversation, so the angle is carried in both ``goal`` (what to
    do) and ``context`` (the shared sub-task it serves) — never assumed shared.
    """
    subtask = _clean_str(subtask)
    angle = _clean_str(angle)
    goal = (
        f"Research this angle of the sub-task and return a concise, "
        f"evidence-backed finding.\n\nSUB-TASK: {subtask}\n\nYOUR ANGLE: {angle}"
    )
    context = (
        "You are one of several independent workers each covering a different "
        "angle of the same research sub-task. Cover ONLY your angle. Cite "
        "sources for every claim; do not assert without evidence. Return your "
        "finding only — no preamble.\n\n"
        f"Shared sub-task: {subtask}"
    )
    return {
        "goal": goal,
        "context": context,
        "toolsets": list(toolsets) if toolsets is not None else list(DEFAULT_WORKER_TOOLSETS),
        "role": "leaf",
    }


def build_worker_tasks(
    subtask: str,
    angles: List[str],
    *,
    max_workers: int = DEFAULT_MAX_WORKERS,
    toolsets: Optional[List[str]] = None,
) -> Tuple[List[Dict[str, Any]], int]:
    """Decompose a sub-task into a capped batch of leaf-worker task dicts.

    Returns ``(tasks, dropped)`` where ``tasks`` is the ``delegate_task``
    ``tasks=[...]`` payload (one entry per kept angle) and ``dropped`` is how many
    angles were discarded for exceeding ``max_workers``. Blank/whitespace angles
    are skipped (they would spawn an empty worker) and do NOT count as dropped.
    Order is preserved so a candidate's ``task_index`` maps back to its angle.
    """
    max_workers = max(1, int(max_workers))
    kept_angles = [a for a in angles if _clean_str(a)]
    dropped = max(0, len(kept_angles) - max_workers)
    kept_angles = kept_angles[:max_workers]
    tasks = [
        build_worker_task(subtask, angle, toolsets=toolsets) for angle in kept_angles
    ]
    return tasks, dropped


def collect_candidates(
    delegate_output: Any,
    angles: Optional[List[str]] = None,
) -> Tuple[List[Dict[str, Any]], int, int]:
    """Turn ``delegate_task``'s return value into evaluator-ready candidates.

    ``delegate_output`` is the JSON ``delegate_task`` returns — either the parsed
    ``{"results": [...]}`` dict or a bare ``[...]`` results list. Each result
    entry is ``{"task_index", "status", "summary", ...}``. This maps every entry
    to a candidate object:

        {
          "index": int,        # the worker's task_index (maps back to its angle)
          "angle": str | None, # the decomposition angle, when provided
          "status": str,       # the worker's delegate_task status
          "ok": bool,          # produced a usable result?
          "candidate": str,    # the worker's summary (the actual finding)
          "scores": {},        # EMPTY — the evaluator (#301 loop) fills this in
        }

    Returns ``(candidates, ok_count, failed_count)``. The ``scores`` dict is left
    empty on purpose: scoring is the evaluator's job, and this module is only the
    collection half of the loop. A failed worker still yields a candidate (so the
    count of attempts is honest) but ``ok=False`` and the evaluator's empty-scores
    rule keeps it from passing the gate. Candidates are ordered by ``task_index``
    so they line up with the angles list.
    """
    if isinstance(delegate_output, dict):
        results = delegate_output.get("results", [])
    elif isinstance(delegate_output, list):
        results = delegate_output
    else:
        results = []
    if not isinstance(results, list):
        results = []

    candidates: List[Dict[str, Any]] = []
    ok_count = 0
    failed_count = 0
    for position, entry in enumerate(results):
        if not isinstance(entry, dict):
            entry = {}
        raw_index = entry.get("task_index", position)
        try:
            index = int(raw_index)
        except (TypeError, ValueError):
            index = position
        status = _clean_str(entry.get("status")).lower()
        summary = _clean_str(entry.get("summary"))
        is_ok = status in _OK_STATUSES and bool(summary)
        if is_ok:
            ok_count += 1
        else:
            failed_count += 1
        angle: Optional[str] = None
        if angles is not None and 0 <= index < len(angles):
            angle = _clean_str(angles[index]) or None
        candidates.append(
            {
                "index": index,
                "angle": angle,
                "status": status,
                "ok": is_ok,
                "candidate": summary,
                "scores": {},
            }
        )
    candidates.sort(key=lambda c: c["index"])
    return candidates, ok_count, failed_count


# ── IO boundary ────────────────────────────────────────────────────────────────
def _read_text(path: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
    try:
        return (Path(path).read_text(encoding="utf-8") if path else sys.stdin.read()), None
    except OSError as exc:
        return None, f"cannot read input: {exc}"


def _load_json(path: Optional[str]) -> Tuple[Any, Optional[str]]:
    raw, err = _read_text(path)
    if err:
        return None, err
    try:
        return json.loads(raw), None
    except ValueError as exc:
        return None, f"input is not valid JSON: {exc}"


def _cmd_build(argv: List[str]) -> int:
    subtask = ""
    angles: List[str] = []
    max_workers = DEFAULT_MAX_WORKERS
    toolsets: Optional[List[str]] = None
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--subtask":
            if i + 1 >= len(argv):
                print("[evolution-orchestrator] --subtask needs a value", file=sys.stderr)
                return 2
            subtask = argv[i + 1]
            i += 2
        elif arg == "--angle":
            if i + 1 >= len(argv):
                print("[evolution-orchestrator] --angle needs a value", file=sys.stderr)
                return 2
            angles.append(argv[i + 1])
            i += 2
        elif arg == "--max-workers":
            if i + 1 >= len(argv):
                print("[evolution-orchestrator] --max-workers needs a value", file=sys.stderr)
                return 2
            try:
                max_workers = int(argv[i + 1])
            except ValueError:
                print(
                    f"[evolution-orchestrator] --max-workers must be an int, got {argv[i + 1]!r}",
                    file=sys.stderr,
                )
                return 2
            i += 2
        elif arg == "--toolsets":
            if i + 1 >= len(argv):
                print("[evolution-orchestrator] --toolsets needs a value", file=sys.stderr)
                return 2
            toolsets = [t.strip() for t in argv[i + 1].split(",") if t.strip()]
            i += 2
        else:
            print(f"[evolution-orchestrator] unknown flag: {arg}", file=sys.stderr)
            return 2
    if not _clean_str(subtask):
        print("[evolution-orchestrator] --subtask is required", file=sys.stderr)
        return 2
    if not [a for a in angles if _clean_str(a)]:
        print("[evolution-orchestrator] at least one --angle is required", file=sys.stderr)
        return 2
    tasks, dropped = build_worker_tasks(
        subtask, angles, max_workers=max_workers, toolsets=toolsets
    )
    print(json.dumps({"tasks": tasks, "dropped": dropped}, ensure_ascii=False))
    return 0


def _cmd_collect(argv: List[str]) -> int:
    path: Optional[str] = None
    angles_path: Optional[str] = None
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--angles":
            if i + 1 >= len(argv):
                print("[evolution-orchestrator] --angles needs a value", file=sys.stderr)
                return 2
            angles_path = argv[i + 1]
            i += 2
        elif arg.startswith("-"):
            print(f"[evolution-orchestrator] unknown flag: {arg}", file=sys.stderr)
            return 2
        else:
            path = arg
            i += 1
    data, err = _load_json(path)
    if err:
        print(f"[evolution-orchestrator] {err}", file=sys.stderr)
        return 2
    angles: Optional[List[str]] = None
    if angles_path is not None:
        adata, aerr = _load_json(angles_path)
        if aerr:
            print(f"[evolution-orchestrator] --angles: {aerr}", file=sys.stderr)
            return 2
        if not isinstance(adata, list):
            print("[evolution-orchestrator] --angles file must be a JSON list", file=sys.stderr)
            return 2
        angles = [a if isinstance(a, str) else "" for a in adata]
    candidates, ok_count, failed_count = collect_candidates(data, angles)
    print(
        json.dumps(
            {"candidates": candidates, "ok": ok_count, "failed": failed_count},
            ensure_ascii=False,
        )
    )
    return 0


def main(argv: List[str]) -> int:
    if len(argv) < 2 or argv[1] in ("-h", "--help"):
        print(
            "usage: evolution_orchestrator.py {build,collect} ...\n"
            "  build   --subtask S --angle A [--angle A ...] [--max-workers N] [--toolsets a,b]\n"
            "  collect [results.json] [--angles angles.json]   (reads stdin if no path)",
            file=sys.stderr,
        )
        return 2
    cmd = argv[1]
    if cmd == "build":
        return _cmd_build(argv[2:])
    if cmd == "collect":
        return _cmd_collect(argv[2:])
    print(f"[evolution-orchestrator] unknown command: {cmd}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
