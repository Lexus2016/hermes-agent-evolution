"""Independent execution-evidence capture (#1716).

Records what the agent *actually* did (tool calls with args + timestamps) to an
append-only JSONL file, separate from the model's narrative, so an explanation
can be cross-checked against recorded evidence. ``record_evidence`` appends a
call; ``verify_claim`` checks whether a claimed action is backed by evidence.
Fail-open: a missing/empty evidence store means "no evidence", never a crash.
"""

import json
import time
from pathlib import Path

from hermes_constants import get_hermes_home

_EVIDENCE_FILENAME = "logs/exec-evidence.jsonl"


def evidence_path() -> Path:
    return get_hermes_home() / _EVIDENCE_FILENAME


def record_evidence(tool_name: str, args: dict | None = None) -> dict:
    """Append one tool-call record to the evidence file and return it."""
    path = evidence_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "ts": int(time.time()),
        "tool": tool_name,
        "args": _summarize(args or {}),
    }
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, sort_keys=True) + "\n")
    return entry


def _summarize(args: dict, max_chars: int = 200) -> str:
    """Serialize args, truncated, so evidence stays small and safe to store."""
    try:
        text = json.dumps(args, sort_keys=True, default=str)
    except Exception:
        text = str(args)
    return text[:max_chars]


def _iter_entries(path: Path):
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            yield json.loads(line)
        except json.JSONDecodeError:
            continue


def verify_claim(tool_name: str, *, path: Path | None = None) -> bool:
    """True if at least one recorded evidence entry names ``tool_name``."""
    try:
        for entry in _iter_entries(path or evidence_path()):
            if entry.get("tool") == tool_name:
                return True
    except Exception:
        pass
    return False


def evidence_count(*, path: Path | None = None) -> int:
    """Number of recorded evidence entries (independent of any narrative)."""
    count = 0
    try:
        for _ in _iter_entries(path or evidence_path()):
            count += 1
    except Exception:
        return 0
    return count
