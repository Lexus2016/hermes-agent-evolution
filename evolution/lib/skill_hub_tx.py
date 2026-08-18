# -*- coding: utf-8 -*-
"""Agentic Transaction Slice 2 — transactional skill hub (#2763).

Child of #2759 (Agentic Transaction, arXiv 2608.13900). Semantic atomicity
for skill-card writes: a skill body becomes VISIBLE in the skill store only
after its validation gate passes. The write is staged to a temp file on the
target's filesystem, the gate runs against the staged body, and only a pass
publishes — via ``os.replace`` (atomic on the same filesystem), so readers
never observe a partially-valid skill. On failure the staged copy is
discarded and the visible store is untouched (rollback by omission).

Combined with the Slice 1 envelope (#2762): when an enabled
``TransactionEnvelope`` is supplied, the publish registers a compensation
that restores the prior body (or removes a newly created card), so a crash
AFTER the atomic replace still rolls the store back to its pre-transaction
state.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from evolution.lib.agentic_tx import TransactionEnvelope

logger = logging.getLogger(__name__)

__all__ = ["StagedSkillResult", "staged_skill_publish"]

# Gate applied to the staged body: True = may become visible.
SkillValidator = Callable[[str], bool]


@dataclass
class StagedSkillResult:
    """Outcome of one staged skill-card write."""

    published: bool
    reason: str
    prior_body: Optional[str] = None  # None also when no card existed before


def staged_skill_publish(
    skill_path: Path,
    new_body: str,
    validator: SkillValidator,
    *,
    envelope: Optional[TransactionEnvelope] = None,
) -> StagedSkillResult:
    """Stage → validate → atomic publish; discard on gate failure.

    ``skill_path`` is the visible card (e.g. ``<skill>/SKILL.md``). The
    staged temp file lives in the same directory (same filesystem → the
    final ``os.replace`` is atomic). A throwing validator is a failure.
    """
    skill_path = Path(skill_path)
    staged = skill_path.with_name(f".{skill_path.name}.tx-staged")
    try:
        staged.write_text(new_body, encoding="utf-8")
    except OSError as exc:
        return StagedSkillResult(False, f"stage failed: {exc}")

    try:
        try:
            gate_passed = bool(validator(new_body))
        except Exception as exc:  # noqa: BLE001 - a throwing gate fails closed
            logger.debug("skill validator raised: %s", exc)
            gate_passed = False
        if not gate_passed:
            return StagedSkillResult(
                False, "validation gate failed — staged write discarded"
            )

        prior_body: Optional[str] = None
        if skill_path.exists():
            try:
                prior_body = skill_path.read_text(encoding="utf-8")
            except OSError:
                prior_body = None

        if envelope is not None and envelope.enabled:
            envelope.register(
                f"skill-card:{skill_path}",
                _restore_compensation(skill_path, prior_body),
            )

        os.replace(staged, skill_path)  # the ONE atomic visibility switch
        return StagedSkillResult(True, "published", prior_body=prior_body)
    finally:
        # Discard the staged copy on every non-published path (and a
        # post-replace finally is a no-op — the file no longer exists).
        try:
            staged.unlink()
        except OSError:
            pass


def _restore_compensation(skill_path: Path, prior_body: Optional[str]) -> Callable[[], None]:
    """Compensation returning the card to its pre-transaction state."""

    def _restore() -> None:
        try:
            if prior_body is None:
                skill_path.unlink(missing_ok=True)
            else:
                skill_path.write_text(prior_body, encoding="utf-8")
        except OSError as exc:
            logger.warning("skill-card restore for %s failed: %s", skill_path, exc)

    return _restore
