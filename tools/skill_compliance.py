"""Skill-Use compliance instrumentation (#2183) — arXiv:2608.04828.

Tracks trigger, compliance, and boundary facets per skill invocation.
Skills may declare ``metadata.hermes.forbidden_tools`` in frontmatter;
this module detects violations when those tools are called.
"""

import contextvars
import logging
import re
from pathlib import Path
from typing import List, Optional, Set

logger = logging.getLogger(__name__)

_compliance_records: dict = {}
_active_skill: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "active_skill", default=None,
)
_tool_calls = contextvars.ContextVar("skill_tool_calls")


def _get_calls() -> list:
    try:
        c = _tool_calls.get()
        return c if isinstance(c, list) else []
    except LookupError:
        return []


def set_active_skill(name: Optional[str]) -> None:
    _active_skill.set(name)
    _tool_calls.set([])


def get_active_skill() -> Optional[str]:
    return _active_skill.get()


def record_tool_call_for_active_skill(tool_name: str) -> None:
    if not _active_skill.get():
        return
    calls = _get_calls()
    calls.append(tool_name)
    _tool_calls.set(calls)


def get_active_skill_tool_calls() -> List[str]:
    return list(_get_calls())


def _read_forbidden_tools(name: str) -> Set[str]:
    try:
        from tools.skill_manager_tool import _find_skill
        found = _find_skill(name)
        if not found:
            return set()
        skill_md = Path(found["path"]) / "SKILL.md"
        if not skill_md.exists():
            return set()
        content = skill_md.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return set()

    if not content.startswith("---"):
        return set()
    end = re.search(r"\n---\s*\n", content[3:])
    if not end:
        return set()
    try:
        import yaml
        parsed = yaml.safe_load(content[3:end.start() + 3])
    except Exception:
        return set()
    if not isinstance(parsed, dict):
        return set()
    meta = parsed.get("metadata") or {}
    hermes = (meta.get("hermes") or {}) if isinstance(meta, dict) else {}
    forbidden = hermes.get("forbidden_tools") or []
    return {str(t).lower() for t in forbidden} if isinstance(forbidden, list) else set()


def check_boundary_violations(skill_name: str, tool_calls_made: List[str]) -> bool:
    forbidden = _read_forbidden_tools(skill_name)
    if not forbidden:
        return False
    observed = {str(t).lower() for t in tool_calls_made}
    return bool(observed & forbidden)


def record_compliance(skill_name: str, *, triggered: bool = True,
                      complied: bool = True, boundary_violated: bool = False) -> None:
    rec = _compliance_records.setdefault(skill_name, {
        "trigger_count": 0, "comply_count": 0, "boundary_violation_count": 0,
    })
    if triggered:
        rec["trigger_count"] += 1
    if complied:
        rec["comply_count"] += 1
    if boundary_violated:
        rec["boundary_violation_count"] += 1


def quality_summary() -> dict:
    return dict(_compliance_records)


def reset_compliance() -> None:
    _compliance_records.clear()
