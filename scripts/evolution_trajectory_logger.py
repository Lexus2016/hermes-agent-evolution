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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

__all__ = [
    "TrajectoryEntry",
    "TrajectoryLog",
    "redact_args",
    "summarize_result",
    "load_trajectory",
    "main",
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


def redact_args(args: Dict[str, Any]) -> Dict[str, Any]:
    """Redact sensitive argument values. Recursively handles nested dicts/lists."""
    if not isinstance(args, dict):
        return args  # type: ignore[return-value]
    out: Dict[str, Any] = {}
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


@dataclass
class TrajectoryEntry:
    tool: str
    args_summary: Dict[str, Any] = field(default_factory=dict)
    result_status: str = "unknown"
    result_summary: str = ""
    timestamp: str = ""
    duration_ms: Optional[int] = None

    def __post_init__(self) -> None:
        if not self.timestamp:
            self.timestamp = datetime.now(timezone.utc).isoformat()

    def to_dict(self) -> Dict[str, Any]:
        d = {
            "timestamp": self.timestamp,
            "tool": self.tool,
            "args_summary": self.args_summary,
            "result_status": self.result_status,
            "result_summary": self.result_summary,
        }
        if self.duration_ms is not None:
            d["duration_ms"] = self.duration_ms
        return d

    @classmethod
    def from_tool_call(
        cls,
        tool: str,
        args: Dict[str, Any],
        result: Any = None,
        status: str = "success",
        duration_ms: Optional[int] = None,
    ) -> "TrajectoryEntry":
        return cls(
            tool=tool,
            args_summary=redact_args(args) if isinstance(args, dict) else {},
            result_status=status,
            result_summary=summarize_result(result),
            duration_ms=duration_ms,
        )


class TrajectoryLog:
    """In-memory trajectory log for a single cron session."""

    def __init__(self, session_id: str = "", date: str = "") -> None:
        self.session_id = session_id
        self.date = date or datetime.now(timezone.utc).strftime("%Y-%m-%d")
        self.entries: List[TrajectoryEntry] = []

    def add(self, entry: TrajectoryEntry) -> None:
        self.entries.append(entry)

    def add_tool_call(
        self,
        tool: str,
        args: Dict[str, Any],
        result: Any = None,
        status: str = "success",
        duration_ms: Optional[int] = None,
    ) -> None:
        self.add(
            TrajectoryEntry.from_tool_call(tool, args, result, status, duration_ms)
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "date": self.date,
            "session_id": self.session_id,
            "entries": [e.to_dict() for e in self.entries],
        }

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

    @property
    def tool_sequence(self) -> List[str]:
        return [e.tool for e in self.entries]

    def tools_used(self) -> set[str]:
        return {e.tool for e in self.entries}

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
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    log = TrajectoryLog(
        session_id=data.get("session_id", ""), date=data.get("date", "")
    )
    for ed in data.get("entries", []):
        if isinstance(ed, dict):
            log.entries.append(
                TrajectoryEntry(
                    tool=ed.get("tool", "unknown"),
                    args_summary=ed.get("args_summary", {}),
                    result_status=ed.get("result_status", "unknown"),
                    result_summary=ed.get("result_summary", ""),
                    timestamp=ed.get("timestamp", ""),
                    duration_ms=ed.get("duration_ms"),
                )
            )
    return log


def main(argv: List[str]) -> int:
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
