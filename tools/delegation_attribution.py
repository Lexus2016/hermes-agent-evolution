# -*- coding: utf-8 -*-
"""Delegation attribution markers (issue #67, slice 1).

Anthropic's multiagent red-team research (the source of #67) found that in
parallel-agent runs, work becomes untraceable the moment two agents touch the
same repo: shared-file conflicts, abandoned PRs, and turf wars all start with
"whose artifact is this?". Slice 1 of the guardrails proposal is the smallest
self-contained fix: give every subagent a traceable origin identity and make
it stamp that identity on everything it produces.

This module defines the canonical marker format. The marker is:

    HERMES-SUBAGENT-ATTRIBUTION subagent_id=<id> parent=<id|root> task_index=<n|-> spawned_at=<ISO8601>

``subagent_id`` is the child's own identity (stable across all its events),
``parent`` is the spawning agent's subagent id (or ``root`` for a
first-level child), and ``spawned_at`` pins the run. The marker is
deliberately a single line with ``key=value`` pairs so it is:

* greppable — ``grep HERMES-SUBAGENT-ATTRIBUTION <file>`` finds every
  artifact a given subagent produced;
* parseable — :func:`parse_attribution_stamp` round-trips it back into a
  structured form for verification tooling;
* embeddable — fits on one line in a file header, commit body, or PR body.

The wiring lives in :mod:`tools.delegate_tool`: every delegated child's
system prompt now carries its attribution stamp plus the instruction to
include the marker in files it creates, commit bodies, and PR titles.

Stdlib-only and import-safe (no side effects on import) — consistent with
the other standalone tools modules.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

__all__ = [
    "ATTRIBUTION_MARKER",
    "AttributionStamp",
    "attribution_prompt_block",
    "build_attribution_stamp",
    "parse_attribution_stamp",
    "stamp_for_artifact_header",
]

#: The canonical marker token. Everything after it is ``key=value`` pairs.
ATTRIBUTION_MARKER = "HERMES-SUBAGENT-ATTRIBUTION"

#: Keys serialized in the canonical stamp line, in order.
_STAMP_KEYS = ("subagent_id", "parent", "task_index", "spawned_at")

#: How a subagent should stamp the artifacts it produces — this text rides
#: in the child's system prompt, so the model stamps rather than improvises.
_STAMPING_INSTRUCTION = (
    "Stamp every artifact you produce with your attribution line (below): "
    "put it in the header of files you create or modify, in the body of any "
    "commit you make, and in the title/body of any PR you open. Use the exact "
    "marker line, unmodified, so work can be traced back to this run."
)


@dataclass(frozen=True)
class AttributionStamp:
    """Structured form of one delegation attribution marker."""

    subagent_id: str
    #: The spawning agent's subagent id, or None for a first-level child
    #: (serialized as ``root``).
    parent_subagent_id: Optional[str] = None
    task_index: Optional[int] = None
    #: ISO-8601 UTC timestamp of the spawn.
    spawned_at: str = ""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def build_attribution_stamp(
    *,
    subagent_id: str,
    parent_subagent_id: Optional[str] = None,
    task_index: Optional[int] = None,
    spawned_at: Optional[str] = None,
) -> str:
    """Build the canonical attribution marker line for a subagent run.

    Parameters mirror the identity available at the child-spawn site in
    :mod:`tools.delegate_tool` (``subagent_id``, ``parent_subagent_id``,
    ``task_index``). ``spawned_at`` defaults to now (UTC).
    """
    if not subagent_id or not isinstance(subagent_id, str):
        raise ValueError("subagent_id is required and must be a non-empty string")
    parent = parent_subagent_id or "root"
    task = "-" if task_index is None else str(task_index)
    ts = spawned_at or _now_iso()
    pairs = [
        f"subagent_id={subagent_id}",
        f"parent={parent}",
        f"task_index={task}",
        f"spawned_at={ts}",
    ]
    return f"{ATTRIBUTION_MARKER} " + " ".join(pairs)


def parse_attribution_stamp(text: Optional[str]) -> Optional[AttributionStamp]:
    """Parse a marker line back into :class:`AttributionStamp`.

    Tolerant by design: returns None for anything that does not start with
    the canonical marker (a file simply may not carry one), and for a
    malformed line (missing subagent_id). Unknown extra keys are ignored so
    the format can grow without breaking older parsers.
    """
    if not isinstance(text, str):
        return None
    stripped = text.strip()
    if not stripped.startswith(ATTRIBUTION_MARKER):
        return None
    body = stripped[len(ATTRIBUTION_MARKER) :].strip()
    fields: dict = {}
    for token in body.split():
        if "=" not in token:
            continue
        key, _, value = token.partition("=")
        fields[key] = value
    subagent_id = fields.get("subagent_id")
    if not subagent_id:
        return None
    parent = fields.get("parent")
    task_raw = fields.get("task_index")
    task_index = None
    if task_raw not in (None, "", "-"):
        try:
            task_index = int(task_raw)
        except ValueError:
            task_index = None
    return AttributionStamp(
        subagent_id=subagent_id,
        parent_subagent_id=None if parent in (None, "root") else parent,
        task_index=task_index,
        spawned_at=fields.get("spawned_at", ""),
    )


def attribution_prompt_block(stamp: str) -> str:
    """The ATTRIBUTION block injected into a child's system prompt.

    ``stamp`` is a canonical marker line from :func:`build_attribution_stamp`.
    """
    return (
        "ATTRIBUTION:\n"
        "You are an attributed subagent run.\n"
        f"Attribution line:\n{stamp}\n" + _STAMPING_INSTRUCTION
    )


def stamp_for_artifact_header(stamp: str) -> str:
    """A one-line comment form of the marker for file headers.

    Produces ``# HERMES-SUBAGENT-ATTRIBUTION ...`` so subagents can embed
    the marker in a file header regardless of the file's language (the
    ``#`` prefix is a valid comment in the overwhelming majority of
    languages, including YAML, TOML, shell, Python, and most configs).

    Parameters
    ----------
    stamp : str
        A canonical marker line from :func:`build_attribution_stamp`.
    """
    return f"# {stamp}"
