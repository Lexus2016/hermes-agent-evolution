# -*- coding: utf-8 -*-
"""Tool synthesis harness — synthesize + validate a tool from scratch (#2259).

Slice A of the In-Situ Self-Evolving paradigm (parent #2248; Yunjue Agent):
tool evolution as the primary capability expansion pathway.

A tool is proposed (as code), tested in a sandbox, and either accepted or
rejected based on binary feedback (did the tool run successfully or not?).

Components:

1. **Tool proposer** — given a task pattern, propose a tool implementation
   (a Python function with a name, description, and body).
2. **Sandbox validator** — run the proposed tool against test inputs in an
   isolated subprocess; capture success/failure.
3. **Binary feedback** — the tool either runs successfully or fails; this
   drives acceptance.
4. **Accepted tools stored in a tool registry** — a simple JSON-backed
   registry of synthesized tools.

New module, no changes to existing tool loading. Diff ≤ 200 lines.
"""

from __future__ import annotations

import json
import logging
import subprocess
import sys
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

__all__ = [
    "SynthesizedTool",
    "ToolProposer",
    "SandboxValidator",
    "ToolRegistry",
]


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class SynthesizedTool:
    """A tool proposed by the synthesizer."""

    name: str
    description: str
    code: str
    accepted: bool = False
    created_at: str = ""

    def __post_init__(self) -> None:
        if not self.created_at:
            self.created_at = _now_iso()

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "SynthesizedTool":
        return cls(
            name=str(d.get("name", "")),
            description=str(d.get("description", "")),
            code=str(d.get("code", "")),
            accepted=bool(d.get("accepted", False)),
            created_at=str(d.get("created_at", "")),
        )


class ToolProposer:
    """Given a task pattern, propose a tool implementation.

    The proposer is deterministic and template-based: it wraps the task
    pattern into a Python function that returns a structured result. This
    is the first increment — a real LLM-backed proposer can replace the
    template later without changing the harness contract.
    """

    @staticmethod
    def propose(task_pattern: str, tool_name: str = "") -> SynthesizedTool:
        """Propose a tool implementation for *task_pattern*."""
        name = tool_name or "synthesized_tool"
        # Sanitize the name to a valid Python identifier.
        name = "".join(c if c.isalnum() or c == "_" else "_" for c in name)
        if not name or name[0].isdigit():
            name = "tool_" + name
        description = f"Synthesized tool for task pattern: {task_pattern.strip()[:80]}"
        code = (
            f"def {name}(task_pattern: str) -> dict:\n"
            f'    """{description}"""\n'
            f'    return {{"task_pattern": task_pattern, "status": "ok"}}\n'
        )
        return SynthesizedTool(name=name, description=description, code=code)


class SandboxValidator:
    """Run a proposed tool against test inputs in an isolated subprocess.

    The validator executes the tool's code in a fresh Python subprocess
    (sandboxed — no access to the parent's state) and returns binary
    feedback: success (exit 0) or failure (non-zero exit / exception).
    """

    @staticmethod
    def validate(tool: SynthesizedTool, test_input: str = "test") -> bool:
        """Run *tool* against *test_input*; return True if it succeeds."""
        # Build a small harness that imports the tool and calls it.
        harness = (
            f"{tool.code}\n"
            f"result = {tool.name}({test_input!r})\n"
            f"assert isinstance(result, dict), 'result must be a dict'\n"
            f"print('OK')\n"
        )
        try:
            proc = subprocess.run(
                [sys.executable, "-c", harness],
                capture_output=True,
                text=True,
                timeout=10,
            )
            return proc.returncode == 0
        except (subprocess.TimeoutExpired, OSError):
            return False


class ToolRegistry:
    """A simple JSON-backed registry of synthesized tools.

    Accepted tools are stored here and can be retrieved by name. The
    registry is a plain JSON file (atomic writes via tempfile + os.replace).
    """

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)

    def _load(self) -> Dict[str, Dict[str, Any]]:
        if not self.path.exists():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def _save(self, data: Dict[str, Dict[str, Any]]) -> None:
        import os
        import tempfile

        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(
            dir=str(self.path.parent), prefix=".registry_", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def store(self, tool: SynthesizedTool) -> None:
        """Store an accepted tool in the registry."""
        data = self._load()
        data[tool.name] = tool.to_dict()
        self._save(data)

    def get(self, name: str) -> Optional[SynthesizedTool]:
        data = self._load()
        entry = data.get(name)
        if not entry:
            return None
        return SynthesizedTool.from_dict(entry)

    def list_names(self) -> List[str]:
        return sorted(self._load().keys())
