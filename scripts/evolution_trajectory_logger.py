#!/usr/bin/env python3
"""Trajectory logging — record cron-cycle action sequences (issue #1215, child of #1203).

Logging-only module: records tool calls during cron sessions as JSON sidecar
in ~/.hermes/evolution/trajectories/<date>.json. No behavioral change.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone, UTC
from pathlib import Path
from typing import Any, Dict, List, Optional

__all__ = [
    "CoordinationEdge",
    "TrajectoryEntry",
    "TrajectoryLog",
    "load_trajectory",
    "main",
    "redact_args",
    "summarize_result",
]

_REDACT_ARG_KEYS = frozenset({
    "token",
    "api_key",
    "apikey",
    "password",
    "passwd",
    "secret",
    "authorization",
    "auth",
    "credential",
    "private_key",
    "session_token",
    "access_token",
    "refresh_token",
    "client_secret",
})
_MAX_RESULT_SUMMARY_LEN = 500


def redact_args(args: dict[str, Any]) -> dict[str, Any]:
    """Redact sensitive argument values. Recursively handles nested dicts/lists."""
    if not isinstance(args, dict):
        return args  # type: ignore[return-value]
    out: dict[str, Any] = {}
    for k, v in args.items():
        if isinstance(k, str) and k.lower() in _REDACT_ARG_KEYS:
            out[k] = "[REDACTED]"
        elif isinstance(v, dict):
            out[k] = redact_args(v)
        elif isinstance(v, list):
            out[k] = [redact_args(x) if isinstance(x, dict) else x for x in v]
        else:
            out[k] = v
    return out


def summarize_result(result: Any) -> str:
    """Short string summary of a tool result, truncated."""
    if result is None:
        return "null"
    text = (
        result
        if isinstance(result, str)
        else json.dumps(result, default=str)
        if not isinstance(result, str)
        else result
    )
    try:
        text = result if isinstance(result, str) else json.dumps(result, default=str)
    except (TypeError, ValueError):
        text = str(result)
    return (
        text[:_MAX_RESULT_SUMMARY_LEN] + "...[truncated]"
        if len(text) > _MAX_RESULT_SUMMARY_LEN
        else text
    )


class CoordinationEdge:
    """A timestamped, typed edge in a multi-agent coordination network (#2996).

    Models one message / file-write / file-read interaction between two actors
    (agents, stages, or a subagent and a shared artifact) with its cost, so
    delegation overhead is measurable and comparable across topologies. This is
    the smallest coherent slice of the "coordination measurement pass" — pure
    logging on the existing TrajectoryLog; no behavior change.
    """

    def __init__(
        self,
        source: str,
        target: str,
        edge_type: str,
        cost_tokens: Optional[int] = None,
        timestamp: str = "",
    ) -> None:
        self.source = source
        self.target = target
        self.edge_type = edge_type
        self.cost_tokens = cost_tokens
        self.timestamp = timestamp or datetime.now(UTC).isoformat()

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "timestamp": self.timestamp,
            "source": self.source,
            "target": self.target,
            "type": self.edge_type,
        }
        if self.cost_tokens is not None:
            d["cost_tokens"] = self.cost_tokens
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> CoordinationEdge:
        return cls(
            source=str(d.get("source", "")),
            target=str(d.get("target", "")),
            edge_type=str(d.get("type", "message")),
            cost_tokens=d.get("cost_tokens"),
            timestamp=str(d.get("timestamp", "")),
        )


@dataclass
class TrajectoryEntry:
    tool: str
    args_summary: dict[str, Any] = field(default_factory=dict)
    result_status: str = "unknown"
    result_summary: str = ""
    timestamp: str = ""
    duration_ms: Optional[int] = None
    reasoning_summary: str = ""

    def __post_init__(self) -> None:
        if not self.timestamp:
            self.timestamp = datetime.now(UTC).isoformat()

    def to_dict(self) -> dict[str, Any]:
        d = {
            "timestamp": self.timestamp,
            "tool": self.tool,
            "args_summary": self.args_summary,
            "result_status": self.result_status,
            "result_summary": self.result_summary,
        }
        if self.duration_ms is not None:
            d["duration_ms"] = self.duration_ms
        # Emitted only when set (same back-compat pattern as `completed` /
        # `task_key` on TrajectoryLog): readers written against the pre-reasoning
        # shape see no new key on the cron-stage trajectories they already
        # handle (issue #112).
        if self.reasoning_summary:
            d["reasoning_summary"] = self.reasoning_summary
        return d

    @classmethod
    def from_tool_call(
        cls,
        tool: str,
        args: dict[str, Any],
        result: Any = None,
        status: str = "success",
        duration_ms: Optional[int] = None,
        reasoning_summary: str = "",
    ) -> TrajectoryEntry:
        return cls(
            tool=tool,
            args_summary=redact_args(args) if isinstance(args, dict) else {},
            result_status=status,
            result_summary=summarize_result(result),
            duration_ms=duration_ms,
            reasoning_summary=reasoning_summary,
        )

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> TrajectoryEntry:
        """Rebuild an entry from its ``to_dict()`` shape (tolerant of gaps).

        Unknown statuses and missing fields are preserved as-is rather than
        defaulted, so a replay sees exactly what was recorded — a recorded
        ``"pending"`` must not silently become ``"unknown"``."""
        return cls(
            tool=str(d.get("tool", "unknown")),
            args_summary=d.get("args_summary", {}) or {},
            result_status=str(d.get("result_status", "unknown")),
            result_summary=str(d.get("result_summary", "")),
            timestamp=str(d.get("timestamp", "")),
            duration_ms=d.get("duration_ms"),
        )


class TrajectoryLog:
    """In-memory trajectory log for a single cron session."""

    def __init__(
        self,
        session_id: str = "",
        date: str = "",
        completed: Optional[bool] = None,
        task_key: str = "",
    ) -> None:
        self.session_id = session_id
        self.date = date or datetime.now(UTC).strftime("%Y-%m-%d")
        self.entries: list[TrajectoryEntry] = []
        # Task-level outcome and pairing key (#1363). Both are optional so the
        # existing cron-stage caller is unaffected, and both are omitted from
        # to_dict() when unset so old readers see the exact shape they expect.
        #
        # ``completed`` is what lets #1359 tell a trajectory worth distilling a
        # heuristic from apart from one that failed; ``task_key`` is an opaque
        # hash of the task descriptor, which is what #1436 pairs a failed and a
        # successful run on. A hash rather than the text because the descriptor
        # is user prose and must not enter the pipeline's store.
        self.completed = completed
        self.task_key = task_key
        # Coordination network edges (#2996). Optional, append-only; empty by
        # default so existing callers are unaffected and the serialized shape
        # of a trajectory with no edges is byte-identical to before.
        self.edges: list[CoordinationEdge] = []

    def add(self, entry: TrajectoryEntry) -> None:
        self.entries.append(entry)

    def add_coordination_edge(
        self,
        source: str,
        target: str,
        edge_type: str = "message",
        cost_tokens: Optional[int] = None,
    ) -> None:
        """Record one message / file-write / file-read edge (#2996).

        Pure logging: measures delegation overhead (who talked to whom, via
        which channel, at what token cost) without changing behavior.
        """
        self.edges.append(CoordinationEdge(source, target, edge_type, cost_tokens))

    def add_tool_call(
        self,
        tool: str,
        args: dict[str, Any],
        result: Any = None,
        status: str = "success",
        duration_ms: Optional[int] = None,
        reasoning_summary: str = "",
    ) -> None:
        self.add(
            TrajectoryEntry.from_tool_call(
                tool, args, result, status, duration_ms, reasoning_summary
            )
        )

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "date": self.date,
            "session_id": self.session_id,
            "entries": [e.to_dict() for e in self.entries],
        }
        # Only emitted when set, so a reader written against the pre-#1363
        # shape sees no new keys on the cron-stage trajectories it already
        # handles.
        if self.completed is not None:
            out["completed"] = bool(self.completed)
        if self.task_key:
            out["task_key"] = self.task_key
        if self.edges:
            out["edges"] = [e.to_dict() for e in self.edges]
        return out

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2, sort_keys=True)

    def save(self, trajectory_dir: Optional[Path] = None) -> Path:
        if trajectory_dir is None:
            trajectory_dir = _default_trajectory_dir()
        trajectory_dir.mkdir(parents=True, exist_ok=True)
        fname = (
            f"{self.date}_{self.session_id}.json"
            if self.session_id
            else f"{self.date}.json"
        )
        path = trajectory_dir / fname
        path.write_text(self.to_json(), encoding="utf-8")
        return path

    def append(self, trajectory_dir: Optional[Path] = None) -> Path:
        """Append this log as one line to a per-session JSONL file.

        :meth:`save` overwrites, which is correct for the cron stage — one
        trajectory per stage run per day. It is wrong for a multi-turn agent
        session: each turn would overwrite the last, so a 12-turn session
        leaves only turn 12 on disk and the other eleven are destroyed. That
        loses exactly the action *sequence* the consumers of #1363 want.

        Appending keeps every turn. One JSON object per line, same shape
        :meth:`to_dict` produces, so a reader can take them one at a time
        without holding the session in memory.
        """
        if trajectory_dir is None:
            trajectory_dir = _default_trajectory_dir()
        trajectory_dir.mkdir(parents=True, exist_ok=True)
        fname = (
            f"{self.date}_{self.session_id}.jsonl"
            if self.session_id
            else f"{self.date}.jsonl"
        )
        path = trajectory_dir / fname
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(self.to_dict(), sort_keys=True) + "\n")
        return path

    @property
    def tool_sequence(self) -> list[str]:
        return [e.tool for e in self.entries]

    def tools_used(self) -> set[str]:
        return {e.tool for e in self.entries}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> TrajectoryLog:
        """Rebuild a log from its ``to_dict()`` shape.

        Shared by ``load_trajectory`` and ``iter_trajectories`` so the
        .json and .jsonl storage formats both round-trip through the same
        reader. Absent optional fields (``completed``, ``task_key``, edges)
        stay absent — a pre-#1363 cron-stage trajectory must read back
        exactly as it was written."""
        log = cls(
            session_id=str(d.get("session_id", "")),
            date=str(d.get("date", "")),
            completed=d.get("completed"),
            task_key=str(d.get("task_key", "")),
        )
        for ed in d.get("entries", []):
            if isinstance(ed, dict):
                log.entries.append(TrajectoryEntry.from_dict(ed))
        for ed in d.get("edges", []):
            if isinstance(ed, dict):
                log.edges.append(CoordinationEdge.from_dict(ed))
        return log

    def failure_count(self) -> int:
        return sum(1 for e in self.entries if e.result_status in ("failure", "error"))


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


def load_trajectory(path: Path) -> Optional[TrajectoryLog]:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    return TrajectoryLog.from_dict(data)


def iter_trajectories(path: Path) -> list[TrajectoryLog]:
    """Load every trajectory in a file, in order — both storage formats.

    ``save()`` writes one pretty-printed JSON object (``.json``);
    ``append()`` writes one compact JSON object per line (``.jsonl``) so a
    multi-turn session keeps every turn. Whole-file parse is tried first
    (covers the .json format), then line-by-line (covers .jsonl). Malformed
    lines are skipped — a torn write must never take a replay down (#85)."""
    try:
        raw = path.read_text(encoding="utf-8-sig")
    except OSError:
        return []
    stripped = raw.strip()
    if not stripped:
        return []
    # Single pretty-printed object (TrajectoryLog.save / load_trajectory).
    try:
        data = json.loads(stripped)
        if isinstance(data, dict):
            return [TrajectoryLog.from_dict(data)]
    except ValueError:
        pass
    # JSONL append format: one compact object per line.
    logs: list[TrajectoryLog] = []
    for line in raw.splitlines():
        s = line.strip()
        if not s:
            continue
        try:
            data = json.loads(s)
        except ValueError:
            continue
        if isinstance(data, dict):
            logs.append(TrajectoryLog.from_dict(data))
    return logs


def main(argv: list[str]) -> int:
    args = argv[1:]
    if not args:
        print("usage: evolution_trajectory_logger.py <date> [--dir DIR] | --file PATH")
        return 2
    if args[0] == "--file" and len(args) > 1:
        log = load_trajectory(Path(args[1]))
        if log is None:
            print(f"error: could not load {args[1]}", file=sys.stderr)
            return 1
        print(log.to_json())
        return 0
    date, traj_dir = args[0], _default_trajectory_dir()
    if "--dir" in args and args.index("--dir") + 1 < len(args):
        traj_dir = Path(args[args.index("--dir") + 1])
    files = sorted(traj_dir.glob(f"{date}*.json"))
    if not files:
        print(f"no trajectory files for {date} in {traj_dir}")
        return 0
    print(f"Trajectory files for {date} ({len(files)}):")
    for f in files:
        log = load_trajectory(f)
        if log:
            tools = ", ".join(sorted(log.tools_used())) or "(none)"
            print(
                f"  {f.name}: {len(log.entries)} entries, tools=[{tools}], failures={log.failure_count()}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
