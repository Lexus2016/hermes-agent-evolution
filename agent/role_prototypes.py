# -*- coding: utf-8 -*-
"""Reusable executable role prototypes for subagent delegation (ExRole, issue #2383).

Inspired by ExRole (arXiv:2608.11949): treats subagent delegation roles as learned,
executable control variables induced from collaboration team trajectories rather than
hand-written static prompt labels.

Key Capabilities:
- RolePrototype representation (role name, toolset recommendations, context keys, goal template).
- Offline / online induction of role prototypes from trajectory traces.
- Future-aware delegation role suggestion based on goal matching.
- JSON persistence and retrieval under Hermes evolution store.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from hermes_constants import get_hermes_home

__all__ = [
    "RolePrototype",
    "role_prototypes_path",
    "load_role_prototypes",
    "save_role_prototypes",
    "induce_role_prototypes_from_trajectories",
    "suggest_delegation_role",
]


@dataclass
class RolePrototype:
    """An executable role prototype induced from team trajectories."""

    name: str
    description: str
    target_tasks: List[str] = field(default_factory=list)
    suggested_tools: List[str] = field(default_factory=list)
    context_keys: List[str] = field(default_factory=list)
    goal_template: str = ""
    recommended_model: str = ""
    success_rate: float = 1.0
    evidence_count: int = 1

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> RolePrototype:
        return cls(
            name=str(d.get("name", "")),
            description=str(d.get("description", "")),
            target_tasks=list(d.get("target_tasks", []) or []),
            suggested_tools=list(d.get("suggested_tools", []) or []),
            context_keys=list(d.get("context_keys", []) or []),
            goal_template=str(d.get("goal_template", "")),
            recommended_model=str(d.get("recommended_model", "")),
            success_rate=float(d.get("success_rate", 1.0)),
            evidence_count=int(d.get("evidence_count", 1)),
        )


def role_prototypes_path() -> Path:
    """Return the absolute path to the stored role prototypes file."""
    return get_hermes_home() / "evolution" / "role_prototypes.json"


def load_role_prototypes() -> List[RolePrototype]:
    """Load role prototypes from disk."""
    path = role_prototypes_path()
    try:
        if not path.is_file():
            return []
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return [
                RolePrototype.from_dict(item) for item in data if isinstance(item, dict)
            ]
    except (OSError, ValueError):
        pass
    return []


def save_role_prototypes(prototypes: Sequence[RolePrototype]) -> bool:
    """Save role prototypes atomically to disk."""
    path = role_prototypes_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = [p.to_dict() for p in prototypes]
        tmp_path = path.with_suffix(".tmp")
        tmp_path.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        tmp_path.replace(path)
        return True
    except OSError:
        return False


def induce_role_prototypes_from_trajectories(
    trajectories: Sequence[Dict[str, Any]],
    *,
    min_evidence: int = 1,
    min_success_rate: float = 0.5,
) -> List[RolePrototype]:
    """Induce reusable role prototypes from a collection of team collaboration traces.

    Each trajectory dictionary is expected to have:
    - ``task_type`` or ``role``: category of task performed
    - ``tools_used`` or ``tool_calls``: list of tools invoked
    - ``success``: bool outcome
    - optional ``context_keys``: list of context fields accessed
    - optional ``model``: model identifier used
    """
    groups: Dict[str, Dict[str, Any]] = {}

    for t in trajectories:
        raw_role = str(t.get("role") or t.get("task_type") or "GeneralWorker").strip()
        role_name = "".join(
            part.capitalize() for part in re.split(r"[_\-\s]+", raw_role) if part
        )
        if not role_name:
            continue

        if role_name not in groups:
            groups[role_name] = {
                "success_count": 0,
                "total_count": 0,
                "tools": {},
                "context_keys": {},
                "models": {},
                "tasks": set(),
            }

        g = groups[role_name]
        g["total_count"] += 1
        is_success = bool(t.get("success", True))
        if is_success:
            g["success_count"] += 1

        task_desc = str(t.get("task_type") or t.get("goal") or "")
        if task_desc:
            g["tasks"].add(task_desc[:60])

        tools = t.get("tools_used") or t.get("tool_calls") or []
        if isinstance(tools, list):
            for tool in tools:
                tool_name = str(tool.get("name") if isinstance(tool, dict) else tool)
                g["tools"][tool_name] = g["tools"].get(tool_name, 0) + 1

        keys = t.get("context_keys") or []
        if isinstance(keys, list):
            for k in keys:
                k_str = str(k)
                g["context_keys"][k_str] = g["context_keys"].get(k_str, 0) + 1

        model = str(t.get("model") or "")
        if model:
            g["models"][model] = g["models"].get(model, 0) + 1

    prototypes: List[RolePrototype] = []
    for role_name, g in groups.items():
        total = g["total_count"]
        if total < min_evidence:
            continue
        success_rate = g["success_count"] / total
        if success_rate < min_success_rate:
            continue

        # Top tools used in >30% of executions
        top_tools = [tool for tool, count in g["tools"].items() if count / total >= 0.3]

        # Top context keys
        top_keys = [k for k, count in g["context_keys"].items() if count / total >= 0.3]

        # Preferred model
        top_model = ""
        if g["models"]:
            top_model = max(g["models"].items(), key=lambda x: x[1])[0]

        proto = RolePrototype(
            name=role_name,
            description=f"Induced role prototype for {role_name} tasks with {success_rate * 100:.0f}% success rate.",
            target_tasks=sorted(g["tasks"])[:5],
            suggested_tools=sorted(top_tools),
            context_keys=sorted(top_keys),
            recommended_model=top_model,
            success_rate=success_rate,
            evidence_count=total,
        )
        prototypes.append(proto)

    return sorted(
        prototypes, key=lambda p: (-p.success_rate, -p.evidence_count, p.name)
    )


def suggest_delegation_role(
    goal: str,
    context: str = "",
    prototypes: Optional[Sequence[RolePrototype]] = None,
) -> Optional[RolePrototype]:
    """Suggest the most suitable induced role prototype for a delegation goal."""
    if prototypes is None:
        prototypes = load_role_prototypes()
    if not prototypes:
        return None

    query_text = f"{goal} {context}".lower()
    words = set(re.findall(r"\b[a-z0-9_]{3,}\b", query_text))
    if not words:
        return None

    best_proto: Optional[RolePrototype] = None
    best_score = 0.0

    for p in prototypes:
        score = 0.0
        # Check role name match
        p_name_lower = p.name.lower()
        if p_name_lower in query_text:
            score += 3.0

        # Check target tasks match
        for task in p.target_tasks:
            task_words = set(re.findall(r"\b[a-z0-9_]{3,}\b", task.lower()))
            overlap = len(words & task_words)
            if overlap:
                score += overlap * 1.5

        # Factor in historical success rate
        score *= 0.5 + 0.5 * p.success_rate

        if score > best_score and score >= 1.5:
            best_score = score
            best_proto = p

    return best_proto
