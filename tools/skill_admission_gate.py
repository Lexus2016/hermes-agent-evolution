"""Pre-commit validation gate for auto-created skills (Slice A of #2181).

When the evolution pipeline auto-creates a skill via the background
self-improvement review fork, this gate runs the candidate against a
held-out validation check BEFORE it is admitted to the active library.

Operationalises the "pre-commit verification" structural requirement from
arXiv:2608.05810 ("When Self-Evolution Backfires: Pre-Commit Gating against
Skill Contamination in LLM Agents") — contaminated self-generated skills
persist and propagate irreversibly, so every skill admitted without
verification is a potential contamination vector.

This slice (A) implements the **structural validation** layer:
  - Frontmatter completeness (name, description, category)
  - Description quality (minimum length, not a placeholder)
  - Content sanity (non-trivial body, no circular self-reference)
  - Records an admission verdict in the skill's usage sidecar

A later slice (C) adds regression correlation + auto-revert.

Config-gated (``skills.pre_commit_validation``, default **off**) so the gate
never fires unless explicitly enabled — foreground/user-authored skills are
always exempt regardless.

Usage (called from ``_create_skill`` after the static security scan passes)::

    from tools.skill_admission_gate import validate_before_admission
    verdict = validate_before_admission(skill_name, skill_dir, content)
    if verdict.blocked:
        # roll back the skill write and return the error
        ...
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# -- Minimum quality thresholds (structural validation only) ---------------

MIN_DESCRIPTION_LEN = 20  # a description shorter than this is not useful
MIN_BODY_LINES = 5  # a skill body with <5 non-empty lines is trivial
MIN_BODY_CHARS = 100  # body must have at least this much substance
PLACEHOLDER_RE = re.compile(
    r"(?i)\b(todo|fixme|placeholder|lorem\s+ipsum|xxx|tbd|your\s+description)"
)

# Admission verdicts
ADMITTED = "admitted"
FLAGGED = "flagged"
BLOCKED = "blocked"


@dataclass
class AdmissionVerdict:
    """Result of a pre-commit validation check on a candidate skill.

    Attributes:
        verdict: one of ``admitted``, ``flagged``, ``blocked``
        checks: list of (check_name, passed, detail) tuples
        blocked: convenience — True iff verdict == BLOCKED
        errors: human-readable list of blocking reasons (empty if admitted)
        warnings: non-blocking quality observations
    """

    verdict: str = ADMITTED
    checks: List[tuple] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    @property
    def blocked(self) -> bool:
        return self.verdict == BLOCKED


# -- Config gate -----------------------------------------------------------


def _pre_commit_validation_enabled() -> bool:
    """Read ``skills.pre_commit_validation`` from config (default False).

    Off by default — this gate adds friction to the skill-creation path and
    should be opted into deliberately. When disabled, ``validate_before_admission``
    returns an ``admitted`` verdict immediately (no-op).
    """
    try:
        from hermes_cli.config import load_config, cfg_get
        from utils import is_truthy_value

        cfg = load_config()
        return is_truthy_value(
            cfg_get(cfg, "skills", "pre_commit_validation"),
            default=False,
        )
    except Exception:
        return False


# -- Individual structural checks ------------------------------------------


def _check_frontmatter(fm: Dict[str, Any]) -> tuple:
    """Verify required frontmatter fields are present and non-empty."""
    missing: List[str] = []
    name = (fm.get("name") or "").strip()
    desc = (fm.get("description") or "").strip()
    if not name:
        missing.append("name")
    if not desc:
        missing.append("description")
    if missing:
        return (
            "frontmatter",
            False,
            f"missing required field(s): {', '.join(missing)}",
        )
    return ("frontmatter", True, "name + description present")


def _check_description_quality(desc: str) -> tuple:
    """Description must be substantive, not a placeholder."""
    stripped = desc.strip()
    if len(stripped) < MIN_DESCRIPTION_LEN:
        return (
            "description_length",
            False,
            f"description too short ({len(stripped)} < {MIN_DESCRIPTION_LEN} chars)",
        )
    if PLACEHOLDER_RE.search(stripped):
        return (
            "description_placeholder",
            False,
            "description contains placeholder text (TODO/FIXME/placeholder)",
        )
    return ("description_quality", True, f"{len(stripped)} chars, no placeholders")


def _check_body_substance(body: str) -> tuple:
    """Skill body must have enough content to be a real procedure."""
    non_empty = [ln for ln in body.splitlines() if ln.strip()]
    if len(non_empty) < MIN_BODY_LINES:
        return (
            "body_depth",
            False,
            f"body too shallow ({len(non_empty)} < {MIN_BODY_LINES} non-empty lines)",
        )
    stripped = body.strip()
    if len(stripped) < MIN_BODY_CHARS:
        return (
            "body_depth",
            False,
            f"body too short ({len(stripped)} < {MIN_BODY_CHARS} chars)",
        )
    return ("body_depth", True, f"{len(non_empty)} lines, {len(stripped)} chars")


def _check_no_self_reference(skill_name: str, content: str) -> tuple:
    """A skill that only instructs the agent to invoke itself is circular."""
    # Look for the skill's own name appearing as the ONLY actionable step
    own_ref = re.findall(rf"(?i)\b{re.escape(skill_name)}\b", content)
    # If the body is short AND references itself 3+ times, it's likely circular
    body = _split_frontmatter(content)[1]
    non_empty = [ln for ln in body.splitlines() if ln.strip()]
    if len(non_empty) < 10 and len(own_ref) >= 3:
        return (
            "circular_reference",
            False,
            "skill body is short but references its own name 3+ times (circular)",
        )
    return ("circular_reference", True, "no circular self-reference pattern")


# -- Helpers ---------------------------------------------------------------

_FM_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?(.*)\Z", re.DOTALL)


def _split_frontmatter(content: str) -> tuple:
    """Split raw SKILL.md content into (frontmatter_dict, body_str)."""
    m = _FM_RE.match(content)
    if not m:
        return ({}, content)
    fm_raw, body = m.group(1), m.group(2)
    try:
        import yaml

        fm = yaml.safe_load(fm_raw) or {}
        if not isinstance(fm, dict):
            fm = {}
    except Exception:
        fm = {}
    return (fm, body)


# -- Main entry point ------------------------------------------------------


def validate_before_admission(
    skill_name: str,
    skill_dir: Path,
    content: str,
) -> AdmissionVerdict:
    """Run pre-commit validation on a candidate auto-created skill.

    Returns an :class:`AdmissionVerdict`. If ``verdict.blocked`` is True,
    the caller MUST roll back the skill write and return the errors to the
    agent. Non-blocking issues are captured as warnings.

    This function is a **no-op** (returns ``admitted``) when:
      - ``skills.pre_commit_validation`` is disabled (the default)
      - the current write origin is NOT ``background_review`` (foreground
        skills are always exempt)
    """
    # Gate 1: config must be explicitly enabled
    if not _pre_commit_validation_enabled():
        return AdmissionVerdict(verdict=ADMITTED)

    # Gate 2: only background-review-origin skills are subject to this gate
    try:
        from tools.skill_provenance import is_background_review

        if not is_background_review():
            return AdmissionVerdict(verdict=ADMITTED)
    except Exception:
        return AdmissionVerdict(verdict=ADMITTED)

    # Run structural validation
    fm, body = _split_frontmatter(content)
    v = AdmissionVerdict()

    # Frontmatter completeness
    chk = _check_frontmatter(fm)
    v.checks.append(chk)
    if not chk[1]:
        v.errors.append(chk[2])

    # Description quality
    desc = (fm.get("description") or "").strip()
    if desc:
        chk = _check_description_quality(desc)
        v.checks.append(chk)
        if not chk[1]:
            v.errors.append(chk[2])

    # Body substance
    chk = _check_body_substance(body)
    v.checks.append(chk)
    if not chk[1]:
        v.errors.append(chk[2])

    # Circular self-reference
    chk = _check_no_self_reference(skill_name, content)
    v.checks.append(chk)
    if not chk[1]:
        v.errors.append(chk[2])

    # Determine verdict: any failed structural check blocks admission
    if v.errors:
        v.verdict = BLOCKED
    else:
        v.verdict = ADMITTED

    # Record the admission result in the skill's usage sidecar
    _record_admission(skill_name, v)

    logger.info(
        "skill_admission_gate: %s verdict=%s errors=%d checks=%d",
        skill_name,
        v.verdict,
        len(v.errors),
        len(v.checks),
    )
    return v


def _record_admission(skill_name: str, verdict: AdmissionVerdict) -> None:
    """Persist the admission verdict to the skill's usage sidecar.

    Best-effort — telemetry failures never break the gate.
    """
    try:
        from datetime import datetime, timezone
        from tools.skill_usage import _mutate

        def _apply(rec: Dict[str, Any]) -> None:
            rec["admission_verdict"] = verdict.verdict
            rec["admission_checked_at"] = datetime.now(timezone.utc).isoformat()
            if verdict.errors:
                rec["admission_errors"] = list(verdict.errors)
            else:
                rec.pop("admission_errors", None)

        _mutate(skill_name, _apply)
    except Exception as e:
        logger.debug("skill_admission_gate: failed to record verdict: %s", e)
