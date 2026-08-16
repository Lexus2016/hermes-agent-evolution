# -*- coding: utf-8 -*-
"""Co-evolution loop — agents and tools improve together (#2262, parent #2251).

Slice B of the dual asset banks: the agent bank (``agent/experience_bank.py``,
#2261) stores ``DelegationPattern`` assets; this module tags every delegation
outcome with the tools the child agent actually used, success-correlated:

* :func:`record_delegation_and_tools` — wraps ``bank.record_delegation_outcome``
  (default: ``agent.experience_bank``) AND aggregates usage tags into
  ``tool_usage_tags.json`` (profile-aware under ``get_hermes_home()``).
* :func:`suggest_configuration` — retrieval-only suggestion combining
  ``bank.find_matching_delegation_patterns`` with top successful tool tags.

Suggestions are consumed OFFLINE by the evolution loop — never applied to a
live agent, since mutating toolsets mid-conversation would break
per-conversation prompt caching (repo policy, AGENTS.md). Both functions fail
open: storage/bank errors are logged at debug and never propagate.
"""

from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from agent import experience_bank
from agent.experience_bank import _atomic_write_json
from hermes_constants import get_hermes_home

logger = logging.getLogger(__name__)

_MAX_TASK_TYPE_LEN = 60
_MIN_SUCCESS_RATE = 0.5  # mirrors find_matching_delegation_patterns default
_MAX_SUGGESTED_TOOLS = 8


def tool_usage_tags_path() -> Path:
    """Return the absolute path to the usage-tagged tool store (lazily resolved)."""
    return get_hermes_home() / "evolution" / "coevolution" / "tool_usage_tags.json"


def _task_type_from_goal(goal: str) -> str:
    """Normalize a delegation goal into an agent-bank ``task_type`` key."""
    text = re.sub(r"\s+", " ", str(goal or "").strip().lower())
    return text[:_MAX_TASK_TYPE_LEN].strip()


def _tool_names(tool_calls: Sequence[Any]) -> List[str]:
    names: List[str] = []
    for call in tool_calls or ():
        if isinstance(call, str):
            name = call.strip()
        elif isinstance(call, dict):
            name = str(call.get("tool") or call.get("name") or "").strip()
        else:
            continue
        if name:
            names.append(name)
    return names


def _load_tags(path: Path) -> Dict[str, Any]:
    """Load the tag store; corrupt or missing files degrade to empty."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            return data
    except (OSError, ValueError) as exc:
        logger.debug("tool-usage tags unreadable at %s: %s", path, exc)
    return {}


def _subdict(parent: Dict[str, Any], key: str) -> Dict[str, Any]:
    """Return ``parent[key]`` if it is a dict, else a fresh empty dict."""
    value = parent.get(key)
    return value if isinstance(value, dict) else {}


def record_delegation_and_tools(
    session_key: str,
    goal: str,
    outcome: Dict[str, Any],
    tool_calls: Sequence[Any],
    bank: Any = None,
    *,
    role: str = "leaf",
    model: str = "",
    tags_path: Optional[Path] = None,
) -> bool:
    """Record one delegation outcome into the agent bank AND the tool tags.

    Fail-open by contract: errors are logged at debug and reported as
    ``False``; they never propagate (the delegation result is already final
    when this is called from ``tools/delegate_tool.py``).
    """
    bank = bank if bank is not None else experience_bank
    task_type = _task_type_from_goal(goal)
    status = str(
        outcome.get("status") or ("completed" if outcome.get("completed") else "failed")
    )
    success = status == "completed"

    ok = True
    try:
        ok = bool(
            bank.record_delegation_outcome(
                task_type=task_type,
                role=role,
                model=model,
                goal_template=str(goal or ""),
                success=success,
            )
        )
    except Exception as exc:  # fail-open: bank must not break delegation
        logger.debug("agent-bank record failed: %s", exc)
        ok = False

    names = _tool_names(tool_calls)
    if not (task_type and names):
        return ok
    path = Path(tags_path) if tags_path is not None else tool_usage_tags_path()
    data = _load_tags(path)
    tasks = _subdict(data, "tasks")
    task_entry = _subdict(tasks, task_type)
    tools = _subdict(task_entry, "tools")
    for name in names:
        stats = _subdict(tools, name)
        stats["total"] = int(stats.get("total", 0)) + 1
        stats["success"] = int(stats.get("success", 0)) + (1 if success else 0)
        tools[name] = stats
    task_entry["tools"] = tools
    task_entry.update(last_session=str(session_key or ""), updated=time.time())
    tasks[task_type] = task_entry
    data["tasks"] = tasks
    written = _atomic_write_json(path, data)  # not `ok and ...` — no short-circuit
    return ok and written


def suggest_configuration(
    goal: str,
    bank: Any = None,
    tags_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """Build a retrieval-only configuration suggestion for a goal.

    STRICTLY read-only: loads the agent bank and the tag store, never writes,
    never mutates live agent configuration (prompt-caching policy). Returns
    ``{"suggested_tools": [...], "matched_patterns": [...]}``.
    """
    bank = bank if bank is not None else experience_bank
    task_type = _task_type_from_goal(goal)

    matched: List[Dict[str, Any]] = []
    try:
        for pattern in bank.find_matching_delegation_patterns(task_type):
            to_dict = getattr(pattern, "to_dict", None)
            matched.append(to_dict() if callable(to_dict) else dict(pattern))
    except Exception as exc:  # fail-open: suggestions are advisory only
        logger.debug("delegation pattern lookup failed: %s", exc)

    suggested: List[str] = []
    if task_type:
        path = Path(tags_path) if tags_path is not None else tool_usage_tags_path()
        totals: Dict[str, List[int]] = {}
        for stored, entry in _subdict(_load_tags(path), "tasks").items():
            stored_type = str(stored).lower().strip()
            similar = stored_type == task_type or stored_type in task_type or task_type in stored_type
            if not similar:
                continue
            for name, stats in _subdict(entry, "tools").items():
                agg = totals.setdefault(str(name), [0, 0])
                agg[0] += int(stats.get("success", 0))
                agg[1] += int(stats.get("total", 0))
        def _rate(agg: List[int]) -> float:
            return agg[0] / agg[1] if agg[1] else 0.0

        ranked = sorted(totals.items(), key=lambda kv: (-_rate(kv[1]), -kv[1][1]))
        suggested = [n for n, agg in ranked if _rate(agg) >= _MIN_SUCCESS_RATE][
            :_MAX_SUGGESTED_TOOLS
        ]
    return {"suggested_tools": suggested, "matched_patterns": matched}
