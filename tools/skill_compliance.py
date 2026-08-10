"""Skill-use trigger/compliance/boundary instrumentation (#2183).

Records per-skill-invocation quality signals to the .usage.json sidecar so the
Curator can see trigger/compliance/boundary-violation rates per skill.

Boundary contract — a skill declares forbidden tools in frontmatter:

    metadata:
      hermes:
        forbidden_tools: [terminal, bash]

All functions are best-effort: a telemetry failure never breaks a tool call.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Sequence

from tools.skill_usage import _find_skill_dir, _mutate, load_usage

logger = logging.getLogger(__name__)


def _read_forbidden_tools(skill_name: str) -> List[str]:
    """Return the ``metadata.hermes.forbidden_tools`` list for a skill."""
    skill_dir = _find_skill_dir(skill_name)
    if skill_dir is None or not (skill_dir / "SKILL.md").exists():
        return []
    try:
        from agent.skill_utils import parse_frontmatter

        fm, _ = parse_frontmatter(
            (skill_dir / "SKILL.md").read_text(encoding="utf-8", errors="replace")
        )
    except Exception as e:
        logger.debug("skill_compliance: failed to parse %s: %s", skill_dir, e)
        return []
    if not isinstance(fm, dict):
        return []
    meta = fm.get("metadata") or {}
    hermes = (meta if isinstance(meta, dict) else {}).get("hermes") or {}
    if not isinstance(hermes, dict):
        return []
    tools = hermes.get("forbidden_tools")
    if not isinstance(tools, list):
        return []
    return [str(t) for t in tools if isinstance(t, (str, int, float))]


def check_boundary_violations(skill_name: str, tool_calls_made: Sequence[str]) -> bool:
    """Return True if any of *tool_calls_made* hits a declared forbidden tool."""
    forbidden = _read_forbidden_tools(skill_name)
    if not forbidden:
        return False
    return bool({str(t) for t in tool_calls_made} & set(forbidden))


def record_compliance(
    skill_name: str, triggered: bool, complied: bool, boundary_violated: bool = False
) -> None:
    """Log a per-invocation quality signal to the .usage.json sidecar.

    Bumps ``trigger_count``/``comply_count``/``boundary_violation_count``.
    Best-effort; failures are logged at DEBUG.
    """
    if not skill_name:
        return

    def _apply(rec: Dict[str, Any]) -> None:
        if triggered:
            rec["trigger_count"] = int(rec.get("trigger_count") or 0) + 1
        if complied:
            rec["comply_count"] = int(rec.get("comply_count") or 0) + 1
        if boundary_violated:
            rec["boundary_violation_count"] = int(rec.get("boundary_violation_count") or 0) + 1

    _mutate(skill_name, _apply)


def quality_summary() -> Dict[str, Dict[str, Any]]:
    """Aggregate per-skill compliance stats for the curator.

    Returns ``{skill_name: {triggers, complies, violations, comply_rate}}`` for
    every skill with a non-zero trigger count.
    """
    summary: Dict[str, Dict[str, Any]] = {}
    for name, rec in load_usage().items():
        if not isinstance(rec, dict):
            continue
        triggers = int(rec.get("trigger_count") or 0)
        if triggers == 0:
            continue
        complies = int(rec.get("comply_count") or 0)
        summary[name] = {
            "triggers": triggers,
            "complies": complies,
            "violations": int(rec.get("boundary_violation_count") or 0),
            "comply_rate": round(complies / triggers, 3),
        }
    return summary