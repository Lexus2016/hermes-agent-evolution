#!/usr/bin/env python3
"""Recorded-input replay gate for skill/prompt/script self-edits (#3517).

A self-edit is merge-eligible only when replaying the inputs it was validated
against reproduces the recorded passing grade. This reuses the existing replay
machinery and adds nothing new: skill edits go through
``evolution_skill_contracts.check_repository_edits`` (routing-contract replay),
session traces go through ``evolution_trace_replay.replay_trajectory``. No
network, no LLM, no new dependency.

The recorded evidence is a small JSON document::

    {"kind": "skill", "root": ".", "base": "origin/main",
     "grade": "pass"}
    {"kind": "trace", "path": "trace.json", "grade": "pass"}

``grade`` is ``pass`` or ``fail`` — the verdict the change claims its replay
produces. Exit 0 when the replay reproduces it (a no-op for a passing one);
exit 1 with one line on stderr when it does not.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for _p in (str(ROOT / "scripts"), str(ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from agent.tool_call_capture import replay_trajectory
from evolution_skill_contracts import check_repository_edits
from evolution_trace_replay import _load_single

EXIT_OK = 0
EXIT_REJECTED = 1
EXIT_BAD_INPUT = 2

_PASSING = {"success"}


def replay_grade(record: dict) -> str:
    """Replay one recorded-input record; return ``pass`` or ``fail``."""
    kind = record.get("kind")
    if kind == "skill":
        root = Path(record.get("root") or ".")
        errors = check_repository_edits(root, record.get("base") or "origin/main")
        return "fail" if errors else "pass"
    if kind == "trace":
        path = record.get("path")
        if not path:
            raise ValueError("trace record requires path")
        log = _load_single(Path(path))
        if log is None:
            raise ValueError(f"could not load trajectory: {path}")
        outcome = replay_trajectory(log).get("outcome")
        return "pass" if outcome in _PASSING else "fail"
    raise ValueError(f"unknown record kind: {kind!r}")


def check_record(record: dict) -> str | None:
    """None when the replay reproduces the recorded grade, else one reason."""
    expected = record.get("grade")
    if expected not in ("pass", "fail"):
        raise ValueError("record.grade must be 'pass' or 'fail'")
    actual = replay_grade(record)
    if actual == expected:
        return None
    return f"replay grade {actual}, recorded {expected}"


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: evolution_replay_gate.py <record.json>", file=sys.stderr)
        return EXIT_BAD_INPUT
    try:
        record = json.loads(Path(argv[1]).read_text(encoding="utf-8"))
        if not isinstance(record, dict):
            raise ValueError("record must be a JSON object")
        reason = check_record(record)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"replay gate: bad record: {exc}", file=sys.stderr)
        return EXIT_BAD_INPUT
    if reason is None:
        return EXIT_OK
    print(f"replay gate: rejected: {reason}", file=sys.stderr)
    return EXIT_REJECTED


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
