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
produces.

Per-turn verdicts keep the replay vocabulary instead of collapsing it:
``success`` \u2192 ``pass``, ``failed`` \u2192 ``fail``, ``incomplete`` \u2192
``incomplete`` (a pending/unknown step: the session was interrupted),
``empty`` \u2192 ``empty`` (nothing to replay). Only ``pass``/``fail`` can
certify a record: an ``incomplete`` or ``empty`` turn is a story that never
finished, so the record is rejected as NOT REPLAYABLE rather than recorded as a
failure. A trace with a torn line is rejected the same way — silently
skipping it would certify a session that was only partly replayed.

Exit codes: 0 the replay reproduces the recorded grade, 1 it does not, 2 the
record itself is unusable, 3 the record is not replayable.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from collections.abc import Callable
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
for _p in (str(ROOT / "scripts"), str(ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from agent.tool_call_capture import replay_trajectory
from evolution_skill_contracts import check_repository_edits
from evolution_trajectory_logger import iter_trajectories

EXIT_OK = 0
EXIT_REJECTED = 1
EXIT_BAD_INPUT = 2
EXIT_NOT_REPLAYABLE = 3

# The record a change ships so the pre-merge path (evolution_merge_gate.py) can
# replay it before the squash lands. Repo-relative, next to the change.
RECORD_PATH = ".evolution/replay-record.json"

# replay_trajectory outcome \u2192 per-turn verdict. "pass"/"fail" are the only
# verdicts a recorded grade can be compared against.
VERDICTS: dict[str, str] = {
    "success": "pass",
    "failed": "fail",
    "incomplete": "incomplete",
    "empty": "empty",
}
CERTIFIABLE = ("pass", "fail")


class NotReplayable(ValueError):
    """The record cannot certify either grade: a turn never finished."""


def _verdict(outcome: Any) -> str:
    """Map a replay outcome to its verdict; unknown outcomes are not guesses."""
    verdict = VERDICTS.get(str(outcome))
    if verdict is None:
        raise NotReplayable(f"unknown replay outcome: {outcome!r}")
    return verdict


def replay_grade(record: dict[str, Any]) -> list[str]:
    """Replay one recorded-input record; one verdict per turn."""
    kind = record.get("kind")
    if kind == "skill":
        root = Path(record.get("root") or ".")
        errors = check_repository_edits(root, record.get("base") or "origin/main")
        return ["fail" if errors else "pass"]
    if kind == "trace":
        # One grade per turn. Session traces are one JSON object per line
        # (TrajectoryLog.append); grading only the first turn accepts a
        # session whose later turn failed, and a torn line silently drops the
        # turn it described — strict loading makes that a hard error.
        path = record.get("path")
        if not path:
            raise ValueError("trace record requires path")
        try:
            logs = iter_trajectories(Path(path), strict=True)
        except ValueError as exc:  # a torn line means a turn was never replayed
            raise NotReplayable(f"trace {path} has an unreadable turn: {exc}") from exc
        if not logs:
            raise NotReplayable(f"no replayable turn in {path}")
        return [_verdict(replay_trajectory(log).get("outcome")) for log in logs]
    raise ValueError(f"unknown record kind: {kind!r}")


def check_record(record: dict[str, Any]) -> str | None:
    """None when every replayed turn reproduces the recorded grade.

    Raises ``NotReplayable`` when a turn came back ``incomplete`` or ``empty``:
    an unfinished session confirms neither a pass nor a fail, so it must not be
    reported as one.
    """
    expected = record.get("grade")
    if expected not in CERTIFIABLE:
        raise ValueError("record.grade must be 'pass' or 'fail'")
    for turn, verdict in enumerate(replay_grade(record), start=1):
        where = f"turn {turn} " if record.get("kind") == "trace" else ""
        if verdict not in CERTIFIABLE:
            raise NotReplayable(
                f"{where}replay outcome {verdict}: the record claims {expected} "
                f"but an {verdict} turn cannot certify it"
            )
        if verdict != expected:
            return f"{where}replay grade {verdict}, recorded {expected}"
    return None


def check_shipped_record(
    record: dict[str, Any],
    *,
    read_text: Callable[[str], str | None],
    workdir: Path | None = None,
) -> list[str]:
    """Check a record shipped inside a PR against the reviewed head.

    ``read_text`` resolves a repo-relative path to its bytes at the reviewed
    head SHA, so the pre-merge path replays exactly what will land. Returns the
    blocking messages (empty list = the record reproduces its grade).
    """
    kind = record.get("kind")
    if kind == "trace":
        path = record.get("path")
        if not path:
            return ["REPLAY_UNVERIFIED: trace record has no path"]
        text = read_text(str(path))
        if text is None:
            return [f"REPLAY_UNVERIFIED: trace {path} is not readable at the reviewed head"]
        target = Path(workdir or tempfile.mkdtemp()) / "shipped-trace.jsonl"
        target.write_text(text, encoding="utf-8")
        record = {**record, "path": str(target)}
    try:
        reason = check_record(record)
    except NotReplayable as exc:
        return [f"REPLAY_NOT_REPLAYABLE: {exc}"]
    except (OSError, ValueError) as exc:
        return [f"REPLAY_UNVERIFIED: {exc}"]
    if reason is None:
        return []
    return [f"REPLAY_NOT_REPRODUCED: {reason}"]


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: evolution_replay_gate.py <record.json>", file=sys.stderr)
        return EXIT_BAD_INPUT
    try:
        record = json.loads(Path(argv[1]).read_text(encoding="utf-8-sig"))
        if not isinstance(record, dict):
            raise ValueError("record must be a JSON object")
        reason = check_record(record)
    except NotReplayable as exc:
        print(f"replay gate: not replayable: {exc}", file=sys.stderr)
        return EXIT_NOT_REPLAYABLE
    except (OSError, ValueError) as exc:  # JSONDecodeError is a ValueError
        print(f"replay gate: bad record: {exc}", file=sys.stderr)
        return EXIT_BAD_INPUT
    if reason is None:
        return EXIT_OK
    print(f"replay gate: rejected: {reason}", file=sys.stderr)
    return EXIT_REJECTED


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
