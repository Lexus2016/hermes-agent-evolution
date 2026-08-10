"""Source-chain provenance tracking for auto-created skills (#2192, Slice A of #2182).

SkillJack (arXiv:2608.03509) shows the attack surface is the **compilation
step**: a poisoned experience record compiled into a durable skill. The
existing ``skill_provenance.py`` only distinguishes background-review vs
foreground writes (a ContextVar). This module adds the **source-chain**
dimension it lacks — recording which tool call / URL / subagent run
produced the experience that compiled into each auto-created skill, and
classifying each source as trusted vs untrusted.

The source chain is accumulated during the background-review fork via
ContextVars and persisted to the skill's usage sidecar at creation time.

Source classification taxonomy:
  - **trusted**: local tool output, vetted research (arxiv, code analysis)
  - **untrusted**: web reads, external tool results, user-provided content
"""

from __future__ import annotations

import contextvars
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# -- Source trust classification -------------------------------------------

# Untrusted source patterns — URLs/schemes that can carry adversarial content
_UNTRUSTED_SCHEMES = {"http://", "https://", "ftp://"}
_UNTRUSTED_TOOLS = {"web_extract", "web_search", "fetch", "read_file_url"}

# Trusted sources — vetted research, local analysis, internal tools
_TRUSTED_TOOLS = {
    "terminal",
    "read_file",
    "search_files",
    "repo_map",
    "execute_code",
    "delegate_task",
    "skill_view",
}
_TRUSTED_DOMAINS = {"arxiv.org", "github.com", "docs.python.org"}


@dataclass
class ProvenanceEntry:
    """A single source in the provenance chain of a skill."""

    source_type: str  # "tool_call", "url", "subagent", "file"
    source_id: str  # tool name, URL, subagent ID, or file path
    trusted: bool = True
    timestamp: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "source_type": self.source_type,
            "source_id": self.source_id,
            "trusted": self.trusted,
            "timestamp": self.timestamp,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "ProvenanceEntry":
        return cls(
            source_type=d.get("source_type", "unknown"),
            source_id=d.get("source_id", ""),
            trusted=d.get("trusted", True),
            timestamp=d.get("timestamp", ""),
        )


# -- ContextVar accumulation -----------------------------------------------

_provenance_chain: contextvars.ContextVar[tuple] = contextvars.ContextVar(
    "skill_source_provenance_chain",
    default=(),
)


def add_provenance_entry(
    source_type: str,
    source_id: str,
    trusted: Optional[bool] = None,
) -> None:
    """Record a source entry in the active provenance chain.

    Called from tool handlers during the background-review fork to record
    what sources fed the experience that will compile into a skill.

    Args:
        source_type: "tool_call", "url", "subagent", or "file"
        source_id: the tool name, URL, subagent ID, or file path
        trusted: override trust classification (auto-classified if None)
    """
    if trusted is None:
        trusted = classify_source(source_type, source_id)
    entry = ProvenanceEntry(
        source_type=source_type,
        source_id=source_id,
        trusted=trusted,
        timestamp=datetime.now(timezone.utc).isoformat(),
    )
    current = _provenance_chain.get()
    _provenance_chain.set(current + (entry,))


def get_provenance_chain() -> List[ProvenanceEntry]:
    """Return the current source-chain (accumulated entries)."""
    return list(_provenance_chain.get())


def reset_provenance_chain() -> None:
    """Clear the accumulated provenance chain (start of a new skill creation)."""
    _provenance_chain.set(())


# -- Source classification --------------------------------------------------


def classify_source(source_type: str, source_id: str) -> bool:
    """Classify a source as trusted (True) or untrusted (False).

    Classification logic:
      - URLs with untrusted schemes → False (unless domain is in trust list)
      - Tool calls with untrusted tool names → False
      - Everything else → True (default-trust for local/internal sources)
    """
    sid = source_id.lower().strip()

    # URL classification
    if source_type == "url" or any(sid.startswith(s) for s in _UNTRUSTED_SCHEMES):
        # Check if domain is in the trusted research set
        for domain in _TRUSTED_DOMAINS:
            if domain in sid:
                return True
        return False

    # Tool call classification
    if source_type == "tool_call":
        if sid in _UNTRUSTED_TOOLS:
            return False
        if sid in _TRUSTED_TOOLS:
            return True
        # Unknown tools default to untrusted (cautious)
        return False

    # Subagent runs inherit trust from their context — default trust
    if source_type == "subagent":
        return True

    # File reads from local filesystem — trusted
    if source_type == "file":
        return True

    # Unknown source type — cautious default
    return False


# -- Persistence to skill sidecar ------------------------------------------


def record_skill_provenance(skill_name: str) -> None:
    """Persist the accumulated source-chain to a skill's usage sidecar.

    Called at skill creation time (in ``_create_skill``) after the skill
    passes all gates. The chain is then reset for the next skill.
    """
    chain = get_provenance_chain()
    if not chain:
        return

    chain_dicts = [e.to_dict() for e in chain]
    has_untrusted = any(not e.trusted for e in chain)

    try:
        from tools.skill_usage import _mutate

        def _apply(rec: Dict[str, Any]) -> None:
            rec["provenance_chain"] = chain_dicts
            rec["provenance_has_untrusted"] = has_untrusted
            rec["provenance_recorded_at"] = datetime.now(timezone.utc).isoformat()

        _mutate(skill_name, _apply)
    except Exception as e:
        logger.debug("record_skill_provenance(%s): %s", skill_name, e)

    # Reset for the next skill creation
    reset_provenance_chain()


def get_skill_provenance(skill_name: str) -> Dict[str, Any]:
    """Return the provenance summary for a skill from its usage sidecar."""
    try:
        from tools.skill_usage import get_record

        rec = get_record(skill_name)
        chain = rec.get("provenance_chain") or []
        if isinstance(chain, list):
            entries = [
                ProvenanceEntry.from_dict(e) for e in chain if isinstance(e, dict)
            ]
        else:
            entries = []
        return {
            "chain": [e.to_dict() for e in entries],
            "has_untrusted": rec.get("provenance_has_untrusted", False),
            "source_count": len(entries),
            "untrusted_count": sum(1 for e in entries if not e.trusted),
            "recorded_at": rec.get("provenance_recorded_at"),
        }
    except Exception:
        return {
            "chain": [],
            "has_untrusted": False,
            "source_count": 0,
            "untrusted_count": 0,
            "recorded_at": None,
        }
