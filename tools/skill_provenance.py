"""Skill write-origin provenance — ContextVar for distinguishing agent-sediment skill writes from foreground user-directed writes.

The curator only consolidates/prunes skills it autonomously created via the
background self-improvement review fork. Skills a user asks a foreground
agent to write belong to the user and must never be auto-curated.

This module exposes a ContextVar that run_agent.py sets before each tool
loop so tool handlers (e.g. skill_manage create) can check whether they
are executing inside the background-review fork.

The signal piggybacks on AIAgent._memory_write_origin, which is already
set to "background_review" for review-fork instances (see
_spawn_background_review in run_agent.py) and defaults to "assistant_tool"
for normal (foreground) agents.

## Source-chain provenance (#2192)

In addition to write-origin, this module tracks the *source chain* — the
sequence of tool calls, web reads, and subagent runs that produced the
experience compiled into an auto-created skill. Each source entry is
classified as trusted (local tool output, vetted research) or untrusted
(web reads, external tool results). The source chain is stored in the
skill's usage record so later slices can taint-flag untrusted provenance.

Usage:
    from tools.skill_provenance import (
        set_current_write_origin,
        reset_current_write_origin,
        get_current_write_origin,
        record_source,
        get_source_chain,
    )

    token = set_current_write_origin("background_review")
    try:
        ...  # tool runs here
    finally:
        reset_current_write_origin(token)

    # inside a tool:
    if get_current_write_origin() == "background_review":
        mark_agent_created(skill_name)
        # source chain is automatically collected from the ContextVar
"""

import contextvars
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from hermes_constants import get_hermes_home

logger = logging.getLogger(__name__)


_write_origin: contextvars.ContextVar[str] = contextvars.ContextVar(
    "skill_write_origin",
    default="foreground",
)

# Source-chain accumulator — collects source entries during a background
# review fork's tool loop. Each entry records what produced the experience
# that may be compiled into a skill.
_source_chain: contextvars.ContextVar[Optional[list]] = contextvars.ContextVar(
    "skill_source_chain",
    default=None,
)

# The sentinel value the background review fork uses; mirrors
# run_agent.py's AIAgent._memory_write_origin override in
# _spawn_background_review().
BACKGROUND_REVIEW = "background_review"

# Source-type classification: trusted vs untrusted.
# Trusted sources are local tool outputs and vetted research materials.
# Untrusted sources are web reads and external tool results that could
# carry adversarial content (SkillJack attack surface).
_TRUSTED_SOURCE_TYPES = frozenset({
    "terminal",  # local shell output
    "read_file",  # local file content
    "search_files",  # local search results
    "execute_code",  # local code execution
    "patch",  # local file edits
    "write_file",  # local file writes
    "delegate_task",  # subagent runs (isolated context)
    "tool_result",  # generic local tool result
})

_UNTRUSTED_SOURCE_TYPES = frozenset({
    "web_search",  # web search results
    "web_extract",  # web page content
    "browser_navigate",  # browser page content
    "browser_action",  # browser interactions
    "external_tool",  # generic external tool result
    "mcp_tool",  # MCP server results (external)
})


def set_current_write_origin(origin: str) -> contextvars.Token[str]:
    """Bind the active write origin to the current context.

    Returns a Token the caller must pass to reset_current_write_origin
    in a finally block.
    """
    return _write_origin.set(origin or "foreground")


def reset_current_write_origin(token: contextvars.Token[str]) -> None:
    """Restore the prior write origin context."""
    _write_origin.reset(token)


def get_current_write_origin() -> str:
    """Return the active write origin.

    Default: "foreground" — any tool call made by a regular (non-review)
    agent, from the CLI, the gateway, cron, or a subagent.

    "background_review" — the self-improvement review fork; only skills
    created under this origin should be marked agent-created for curator
    management.
    """
    return _write_origin.get()


def is_background_review() -> bool:
    """Convenience: True iff the current write origin is the background
    review fork."""
    return get_current_write_origin() == BACKGROUND_REVIEW


# ---------------------------------------------------------------------------
# Source-chain provenance (#2192)
# ---------------------------------------------------------------------------


def init_source_chain() -> None:
    """Initialize the source-chain accumulator for the current context.

    Called at the start of a background-review fork's tool loop so that
    source entries can be collected as tools execute.
    """
    _source_chain.set([])


def record_source(
    source_type: str,
    source_ref: str = "",
    detail: str = "",
) -> None:
    """Record a source entry in the current context's source chain.

    Called from tool handlers (or a post-tool hook) to note that a
    particular tool call contributed to the experience that may be
    compiled into a skill. Only records if a source-chain accumulator
    is active (i.e., inside a background-review fork).

    Args:
        source_type: The tool name or source category (e.g. "terminal",
            "web_search", "read_file").
        source_ref: A compact reference to the source — a file path,
            URL, or tool-call identifier. Truncated to 200 chars.
        detail: Optional one-line detail (e.g. "grep for X"). Truncated
            to 200 chars.
    """
    chain = _source_chain.get()
    if chain is None:
        return  # not in a background-review context
    entry = {
        "source_type": source_type[:80],
        "source_ref": (source_ref or "")[:200],
        "detail": (detail or "")[:200],
        "trusted": _classify_source(source_type),
        "recorded_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    chain.append(entry)


def get_source_chain() -> List[Dict[str, Any]]:
    """Return the source chain accumulated in the current context.

    Returns an empty list if no source-chain accumulator is active.
    """
    chain = _source_chain.get()
    return list(chain) if chain else []


def _classify_source(source_type: str) -> bool:
    """Classify a source type as trusted (True) or untrusted (False).

    Unknown source types default to untrusted (fail-safe).
    """
    if source_type in _TRUSTED_SOURCE_TYPES:
        return True
    if source_type in _UNTRUSTED_SOURCE_TYPES:
        return False
    # Unknown → untrusted (fail-safe: better to over-flag than miss an
    # adversarial source).
    return False


def _source_chain_file() -> Path:
    """Return the path to the source-chain sidecar JSON."""
    return get_hermes_home() / "skills" / ".source_chains.json"


def save_source_chain(skill_name: str, chain: List[Dict[str, Any]]) -> None:
    """Persist the source chain for a skill to the sidecar file.

    Called from the skill-admission path (skill_manage create) when a
    background-review-created skill is committed. Best-effort; failures
    log at DEBUG and return silently.
    """
    if not skill_name or not chain:
        return
    try:
        path = _source_chain_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {}
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                data = {}
        if not isinstance(data, dict):
            data = {}
        data[skill_name] = {
            "chain": chain,
            "saved_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)
    except Exception as e:
        logger.debug("save_source_chain(%s) failed: %s", skill_name, e, exc_info=True)


def load_source_chain(skill_name: str) -> Optional[Dict[str, Any]]:
    """Load the source chain for a skill from the sidecar file.

    Returns None if no source chain is recorded for the skill.
    """
    if not skill_name:
        return None
    path = _source_chain_file()
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data.get(skill_name)
    except (OSError, json.JSONDecodeError) as e:
        logger.debug("load_source_chain(%s) failed: %s", skill_name, e)
    return None


def get_source_chain_summary(skill_name: str) -> Dict[str, Any]:
    """Return a human-readable summary of a skill's source chain.

    Used by the curator / skill-listing to surface provenance info.
    """
    record = load_source_chain(skill_name)
    if not record:
        return {"has_source_chain": False}
    chain = record.get("chain", [])
    trusted_count = sum(1 for e in chain if e.get("trusted"))
    untrusted_count = len(chain) - trusted_count
    return {
        "has_source_chain": True,
        "source_count": len(chain),
        "trusted_sources": trusted_count,
        "untrusted_sources": untrusted_count,
        "all_trusted": untrusted_count == 0,
        "saved_at": record.get("saved_at"),
        "sources": [
            {
                "source_type": e.get("source_type"),
                "source_ref": e.get("source_ref"),
                "trusted": e.get("trusted"),
            }
            for e in chain
        ],
    }
