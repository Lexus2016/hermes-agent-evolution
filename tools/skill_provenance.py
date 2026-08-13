"""Skill write-origin provenance — ContextVar for distinguishing agent-sediment
skill writes from foreground user-directed writes, plus source-chain tracking
for background-review-created skills.

The source chain records which tool calls / URLs / subagent runs produced the
experience that compiled into a skill. This is the SkillJack defense (arXiv:2608.03509):
later slices can taint-flag untrusted provenance sources.
"""

import contextvars
import logging
from typing import Any, Dict, List

logger = logging.getLogger(__name__)

_write_origin: contextvars.ContextVar[str] = contextvars.ContextVar(
    "skill_write_origin",
    default="foreground",
)

# Source-chain accumulator — list of source entries recorded during the
# current background-review fork. Each entry: {source_type, source_id, trusted}.
_source_chain: contextvars.ContextVar[list | None] = contextvars.ContextVar(
    "skill_source_chain",
    default=None,
)

BACKGROUND_REVIEW = "background_review"

# Source types classified as trusted vs untrusted (SkillJack taxonomy).
_TRUSTED_TYPES = frozenset({"terminal", "read_file", "search_files", "execute_code"})


def set_current_write_origin(origin: str) -> contextvars.Token[str]:
    return _write_origin.set(origin or "foreground")


def reset_current_write_origin(token: contextvars.Token[str]) -> None:
    _write_origin.reset(token)


def get_current_write_origin() -> str:
    return _write_origin.get()


def is_background_review() -> bool:
    return get_current_write_origin() == BACKGROUND_REVIEW


def init_source_chain() -> contextvars.Token:
    """Start accumulating source entries. Call at background-review fork start."""
    return _source_chain.set([])


def reset_source_chain(token: contextvars.Token) -> None:
    """Clear the accumulator. Call at fork end."""
    _source_chain.reset(token)


def add_provenance_entry(source_type: str, source_id: str = "") -> None:
    """Record a source entry in the current background-review chain.

    Called from the post-tool-dispatch path in model_tools.py. Only records
    when inside a background-review fork (is_background_review() is True and
    a chain has been initialized). Classifies the source as trusted/untrusted.
    """
    if not is_background_review():
        return
    chain = _source_chain.get()
    if chain is None:
        return
    trusted = source_type in _TRUSTED_TYPES
    chain.append({
        "source_type": source_type,
        "source_id": (source_id or "")[:200],
        "trusted": trusted,
    })


def get_recorded_chain() -> List[Dict[str, Any]]:
    """Return the current source chain (for skill_manage to attach at creation)."""
    chain = _source_chain.get()
    return list(chain) if chain else []


def get_skill_provenance(skill_name: str) -> List[Dict[str, Any]]:
    """Retrieve the persisted source chain for a skill.

    Reads from the skill's usage record in .usage.json (stored under the
    ``source_chain`` key by skill_manage at creation time).
    """
    try:
        from tools.skill_usage import get_record

        rec = get_record(skill_name)
        chain = rec.get("source_chain") or []
        return chain if isinstance(chain, list) else []
    except Exception:
        return []


def provenance_ok(
    chain: list | None = None,
) -> tuple[bool, str]:
    """Provenance gate for provisional→trusted skill promotion (#2288).

    PoisonedEvolution (arXiv:2608.05563) shows that trajectory-poisoning
    attacks embed malicious behaviors via seemingly-ordinary evidence that
    *looks* causally useful.  The distinctive defense lever is **attribution**:
    a skill promoted to trusted must carry verifiable source provenance.

    Returns ``(ok, reason)``.  ``ok=False`` blocks promotion.

    A background-review skill must have a recorded source chain whose entries
    include at least one **trusted** source (terminal, read_file, search_files,
    execute_code — the SkillJack taxonomy).  An empty chain or one with *zero*
    trusted sources is rejected — the skill has no verifiable attribution.

    Accepts the *live* chain (from ``get_recorded_chain``) so the gate can run
    BEFORE the chain is persisted to ``.usage.json``.
    """
    entries = chain if chain is not None else get_recorded_chain()
    if not entries:
        return False, "no source_chain recorded — skill has no provenance attribution"
    trusted = [e for e in entries if e.get("trusted")]
    if not trusted:
        untrusted_types = sorted({e.get("source_type", "?") for e in entries})
        return (
            False,
            f"source_chain has no trusted sources (all untrusted: {untrusted_types})",
        )
    return True, ""


def record_promotion(skill_name: str, reason: str = "") -> None:
    """Stamp the usage record that this skill passed the provenance gate and
    was promoted to trusted.  Best-effort audit trail (#2288).
    """
    try:
        from datetime import datetime, timezone
        from tools.skill_usage import _mutate

        def _apply(rec: dict) -> None:
            rec["promoted_at"] = datetime.now(timezone.utc).isoformat()
            rec["promotion_reason"] = (reason or "provenance_ok")[:200]

        _mutate(skill_name, _apply)
    except Exception as exc:
        logger.warning("record_promotion(%s) failed: %s", skill_name, exc)
