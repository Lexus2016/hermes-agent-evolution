#!/usr/bin/env python3
"""Trajectory logger for cron/evolution sessions.

Records the sequence of tool calls (name, key args, result summary, pass/fail)
taken during a cron session as a JSON sidecar in
``~/.hermes/evolution/trajectories/<date>.json``.

This is increment 1 of 3 for issue #1215 (parent #1203 — trajectory-level
safety monitoring). The logger is exercised via its CLI entry point so it is
not dead code; increments 2 (pattern detection) and 3 (alerting) will consume
the sidecar files.

Usage (CLI):
    python -m scripts.evolution_trajectory_logger --session <id> --tool-call \\
        --tool-name terminal --args '{"command":"ls"}' --result "ok" --status pass

    python -m scripts.evolution_trajectory_logger --session <id> --flush

Usage (library):
    from scripts.evolution_trajectory_logger import TrajectoryLogger
    logger = TrajectoryLogger(session_id="abc123")
    logger.record("terminal", {"command": "ls"}, "ok", "pass")
    logger.flush()
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional


def _get_trajectories_dir() -> Path:
    """Return the trajectories directory under HERMES_HOME."""
    hermes_home = os.environ.get("HERMES_HOME", str(Path.home() / ".hermes"))
    d = Path(hermes_home) / "evolution" / "trajectories"
    d.mkdir(parents=True, exist_ok=True)
    return d


_REDACTED_KEYS = frozenset({
    "api_key",
    "token",
    "password",
    "secret",
    "authorization",
    "key",
    "credential",
    "passwd",
})


def _redact_args(args: Dict[str, Any]) -> Dict[str, Any]:
    """Redact secret-like keys from tool args before logging."""
    if not isinstance(args, dict):
        return {}
    safe = {}
    for k, v in args.items():
        if k.lower() in _REDACTED_KEYS:
            safe[k] = "[REDACTED]"
        elif isinstance(v, str) and len(v) > 500:
            safe[k] = v[:500] + "...[truncated]"
        else:
            safe[k] = v
    return safe


class TrajectoryLogger:
    """Accumulate tool-call records for a session and persist as JSON."""

    def __init__(self, session_id: str):
        self.session_id = session_id
        self._entries: List[Dict[str, Any]] = []
        self._start_ts = time.time()

    def record(
        self,
        tool_name: str,
        args: Dict[str, Any],
        result_summary: str,
        status: str,
    ) -> None:
        """Record a single tool call in the trajectory."""
        self._entries.append({
            "timestamp": time.time(),
            "tool": tool_name,
            "args": _redact_args(args),
            "result_summary": result_summary[:500]
            if isinstance(result_summary, str)
            else str(result_summary)[:500],
            "status": status,
        })

    def flush(self) -> Path:
        """Write the accumulated trajectory to a dated JSON sidecar.

        Returns the path to the written file.
        """
        date_str = time.strftime("%Y-%m-%d", time.localtime())
        out_dir = _get_trajectories_dir()
        out_file = out_dir / f"{date_str}.json"

        # Merge with existing entries for the same date
        existing: List[Dict[str, Any]] = []
        if out_file.exists():
            try:
                data = json.loads(out_file.read_text(encoding="utf-8"))
                existing = data if isinstance(data, list) else []
            except (json.JSONDecodeError, OSError):
                existing = []

        existing.append({
            "session_id": self.session_id,
            "start_ts": self._start_ts,
            "flush_ts": time.time(),
            "entries": self._entries,
        })

        out_file.write_text(
            json.dumps(existing, indent=2, default=str),
            encoding="utf-8",
        )
        self._entries.clear()
        return out_file

    @property
    def entry_count(self) -> int:
        return len(self._entries)


def main(argv: Optional[List[str]] = None) -> int:
    """CLI entry point for standalone trajectory recording."""
    parser = argparse.ArgumentParser(
        description="Record tool-call trajectories for cron/evolution sessions.",
    )
    parser.add_argument("--session", required=True, help="Session ID")
    parser.add_argument(
        "--tool-call",
        action="store_true",
        help="Record a single tool call (requires --tool-name, --args, --result, --status)",
    )
    parser.add_argument("--tool-name", default="", help="Name of the tool called")
    parser.add_argument("--args", default="{}", help="Tool arguments as JSON string")
    parser.add_argument("--result", default="", help="Result summary")
    parser.add_argument(
        "--status",
        default="pass",
        choices=["pass", "fail"],
        help="Pass/fail status of the tool call",
    )
    parser.add_argument(
        "--flush", action="store_true", help="Flush accumulated entries to disk"
    )
    args = parser.parse_args(argv)

    logger = TrajectoryLogger(session_id=args.session)

    if args.tool_call:
        try:
            parsed_args = json.loads(args.args)
        except json.JSONDecodeError:
            parsed_args = {"raw": args.args}
        logger.record(args.tool_name, parsed_args, args.result, args.status)
        print(f"Recorded tool call: {args.tool_name} ({args.status})")

    if args.flush:
        path = logger.flush()
        print(f"Trajectory flushed to: {path}")
        return 0

    if not args.tool_call and not args.flush:
        parser.print_help()
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
