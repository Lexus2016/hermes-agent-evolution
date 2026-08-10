"""Per-invocation skill quality instrumentation (#2183).

Instruments three facets of skill use per arXiv:2608.04828 (Skill-Use
benchmark):

1. **Trigger** — was a relevant skill retrieved/invoked? (bump_use already
   records this; here we add an explicit trigger-quality signal.)
2. **Compliance** — was the skill's procedure followed? (recorded post-turn
   by the evaluator or agent loop)
3. **Boundary** — were forbidden operations avoided? (checked against the
   skill's ``forbidden_operations`` frontmatter field)

The signals are stored in the skill's usage sidecar (``.usage.json``)
alongside existing telemetry, giving the evolution pipeline a per-skill
quality signal beyond pass/fail.

This is **additive instrumentation** — it never blocks or interferes with
skill invocation. All recording is best-effort.

SKILL.md frontmatter extension::

    ---
    name: my-skill
    description: ...
    forbidden_operations:
      - "rm -rf /"
      - "os.system('curl')"
    ---
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Signal types
SIGNAL_TRIGGER = "trigger"
SIGNAL_COMPLIANCE = "compliance"
SIGNAL_BOUNDARY = "boundary"

_VALID_SIGNALS = {SIGNAL_TRIGGER, SIGNAL_COMPLIANCE, SIGNAL_BOUNDARY}

# Aggregated quality record fields stored in .usage.json:
#   trigger_count:    total times the skill was invoked
#   trigger_last_at:  ISO timestamp of last trigger
#   compliance_score: running average [0.0, 1.0]
#   compliance_count: number of compliance evaluations
#   boundary_violations: total forbidden-op matches detected
#   boundary_last_violation_at: ISO timestamp or None


def record_trigger(skill_name: str) -> None:
    """Record that a skill was triggered (retrieved + invoked by the agent).

    Called from the skill_view/use path when the agent actively loads a
    skill to act on it. This is the Trigger facet — a skill that is never
    triggered has zero value regardless of procedure quality.
    """
    try:
        from tools.skill_usage import _mutate

        def _apply(rec: Dict[str, Any]) -> None:
            rec["trigger_count"] = int(rec.get("trigger_count") or 0) + 1
            rec["trigger_last_at"] = datetime.now(timezone.utc).isoformat()

        _mutate(skill_name, _apply)
    except Exception as e:
        logger.debug("skill_quality_signal.record_trigger(%s): %s", skill_name, e)


def record_compliance(skill_name: str, score: float) -> None:
    """Record a compliance evaluation for a skill invocation.

    Args:
        skill_name: the skill that was evaluated
        score: 0.0–1.0 — did the agent follow the prescribed procedure?

    The score is accumulated as a running average in the sidecar.
    """
    score = max(0.0, min(1.0, float(score)))
    try:
        from tools.skill_usage import _mutate

        def _apply(rec: Dict[str, Any]) -> None:
            count = int(rec.get("compliance_count") or 0) + 1
            prev_avg = float(rec.get("compliance_score") or 0.0)
            # Incremental average
            new_avg = prev_avg + (score - prev_avg) / count
            rec["compliance_count"] = count
            rec["compliance_score"] = round(new_avg, 4)
            rec["compliance_last_at"] = datetime.now(timezone.utc).isoformat()

        _mutate(skill_name, _apply)
    except Exception as e:
        logger.debug("skill_quality_signal.record_compliance(%s): %s", skill_name, e)


def record_boundary_violation(skill_name: str, operation: str) -> None:
    """Record that a forbidden operation was detected during skill use.

    Args:
        skill_name: the skill whose boundary was violated
        operation: the forbidden operation that was detected
    """
    try:
        from tools.skill_usage import _mutate

        def _apply(rec: Dict[str, Any]) -> None:
            rec["boundary_violations"] = int(rec.get("boundary_violations") or 0) + 1
            rec["boundary_last_violation_at"] = datetime.now(timezone.utc).isoformat()
            recent = rec.get("boundary_recent") or []
            if not isinstance(recent, list):
                recent = []
            recent.insert(0, operation[:200])
            rec["boundary_recent"] = recent[:10]  # keep last 10

        _mutate(skill_name, _apply)
    except Exception as e:
        logger.debug(
            "skill_quality_signal.record_boundary_violation(%s): %s", skill_name, e
        )


# -- Forbidden operations parsing ------------------------------------------


def parse_forbidden_operations(skill_md_path: Path) -> List[str]:
    """Extract the ``forbidden_operations`` list from a SKILL.md frontmatter.

    Returns an empty list if the field is absent or the file is unreadable.
    """
    try:
        import yaml

        content = skill_md_path.read_text(encoding="utf-8")
        m = re.match(r"\A---\s*\n(.*?)\n---\s*\n?", content, re.DOTALL)
        if not m:
            return []
        fm = yaml.safe_load(m.group(1)) or {}
        if not isinstance(fm, dict):
            return []
        ops = fm.get("forbidden_operations")
        if not isinstance(ops, list):
            return []
        return [str(op).strip() for op in ops if str(op).strip()]
    except Exception as e:
        logger.debug("parse_forbidden_operations(%s): %s", skill_md_path, e)
        return []


def check_boundary_violations(
    skill_name: str,
    skill_md_path: Path,
    agent_output: str,
) -> List[str]:
    """Check agent output against a skill's forbidden operations.

    Args:
        skill_name: the skill being evaluated
        skill_md_path: path to the skill's SKILL.md
        agent_output: the tool calls / commands / code the agent produced

    Returns a list of matched forbidden operations (empty if none matched).
    Each match is also recorded via ``record_boundary_violation``.
    """
    forbidden = parse_forbidden_operations(skill_md_path)
    if not forbidden:
        return []

    violations: List[str] = []
    for op in forbidden:
        # Use substring matching for literal patterns; treat as regex if
        # it contains regex metacharacters and compiles cleanly
        try:
            if any(c in op for c in r"\.+*?[]{}|^$()|"):
                if re.search(op, agent_output):
                    violations.append(op)
            elif op in agent_output:
                violations.append(op)
        except re.error:
            # Fall back to literal match on bad regex
            if op in agent_output:
                violations.append(op)

    for v in violations:
        record_boundary_violation(skill_name, v)

    return violations


# -- Quality summary -------------------------------------------------------


def quality_summary(skill_name: str) -> Dict[str, Any]:
    """Return a summary of the three-facet quality signals for a skill.

    Useful for the evolution pipeline to identify skills with low trigger
    rates (bad descriptions), low compliance (bad procedures), or boundary
    violations (unsafe skills).
    """
    try:
        from tools.skill_usage import get_record

        rec = get_record(skill_name)
        return {
            "trigger_count": int(rec.get("trigger_count") or 0),
            "trigger_last_at": rec.get("trigger_last_at"),
            "compliance_score": float(rec.get("compliance_score") or 0.0),
            "compliance_count": int(rec.get("compliance_count") or 0),
            "boundary_violations": int(rec.get("boundary_violations") or 0),
            "boundary_last_violation_at": rec.get("boundary_last_violation_at"),
        }
    except Exception:
        return {
            "trigger_count": 0,
            "trigger_last_at": None,
            "compliance_score": 0.0,
            "compliance_count": 0,
            "boundary_violations": 0,
            "boundary_last_violation_at": None,
        }
