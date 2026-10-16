"""Governed shared memory with scope, provenance, supersession, and redistribution (Issue #2488, Slice B, MemClaw).

Mitigates multi-agent shared memory failure modes:
1. Leakage (strict scope boundaries)
2. Stale propagation (temporal supersession tracking)
3. Contradiction persistence (explicit supersedes relations)
4. Provenance collapse (full author/tool provenance tracing)
5. Cross-class leakage (reader-class authorization enforced before retrieval)
"""

from __future__ import annotations

import logging
import time
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class MemoryScope(str, Enum):
    """Scope boundaries for governed shared memory."""

    LOCAL = "local"
    SUBAGENT = "subagent"
    TASK = "task"
    GLOBAL = "global"


@dataclass
class MemoryProvenance:
    """Provenance tracking for a memory item."""

    author_subagent_id: str
    source_tool: str = ""
    sources: list[str] = field(default_factory=list)
    timestamp_ms: float = field(default_factory=lambda: time.time() * 1000.0)
    confidence: float = 1.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class FreshnessSignal:
    """Retrieval-time freshness verdict for a memory key (#3336).

    Attributes:
        status: One of ``"current"`` (record exists and is active),
            ``"superseded"`` (record exists but a newer record replaced it),
            or ``"unknown"`` (no record under this key).
        superseded_by: Key of the newer record, when ``status`` is
            ``"superseded"``.
        superseded_at_ms: Epoch-ms timestamp of the supersession, when known.
    """

    status: str
    superseded_by: Optional[str] = None
    superseded_at_ms: Optional[float] = None


@dataclass
class GovernedMemoryRecord:
    """A governed memory entry with scope, provenance, and supersession links."""

    key: str
    value: Any
    scope: str = MemoryScope.TASK.value
    provenance: MemoryProvenance = field(
        default_factory=lambda: MemoryProvenance(author_subagent_id="unknown")
    )
    supersedes_key: Optional[str] = None
    superseded_by: Optional[str] = None
    is_active: bool = True
    reader_class: Optional[str] = None
    created_at_ms: float = field(default_factory=lambda: time.time() * 1000.0)
    updated_at_ms: float = field(default_factory=lambda: time.time() * 1000.0)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class GovernedSharedMemory:
    """Central store managing governed memory records with provenance and redistribution."""

    def __init__(self) -> None:
        self._records: dict[str, GovernedMemoryRecord] = {}

    def write(
        self,
        key: str,
        value: Any,
        author_id: str,
        scope: str = MemoryScope.TASK.value,
        source_tool: str = "",
        sources: Optional[list[str]] = None,
        supersedes_key: Optional[str] = None,
        confidence: float = 1.0,
        reader_class: Optional[str] = None,
    ) -> GovernedMemoryRecord:
        """Write a new governed memory record with provenance and handle supersession.

        ``reader_class`` tags the record with a hard authorization class (e.g.
        "orchestrator" vs "stage-agent"); only readers presenting the same class
        may retrieve it (see ``read``). ``None`` (default) means unrestricted,
        preserving legacy behavior.

        Restrictions propagate through derivations: a record that supersedes a
        restricted record inherits that restriction, so a summary cannot launder
        a source it was derived from. An explicit ``reader_class`` only applies
        when the superseded record is unrestricted — otherwise the superseded
        record's restriction wins (most-restricted-source rule).
        """
        now_ms = time.time() * 1000.0
        prov = MemoryProvenance(
            author_subagent_id=author_id,
            source_tool=source_tool,
            sources=sources or [],
            timestamp_ms=now_ms,
            confidence=confidence,
        )

        # Handle supersession of existing record
        inherited_reader_class = reader_class
        if supersedes_key and supersedes_key in self._records:
            old_record = self._records[supersedes_key]
            old_record.is_active = False
            old_record.superseded_by = key
            old_record.updated_at_ms = now_ms
            if old_record.reader_class is not None:
                inherited_reader_class = old_record.reader_class

        record = GovernedMemoryRecord(
            key=key,
            value=value,
            scope=scope.lower() if isinstance(scope, str) else MemoryScope.TASK.value,
            provenance=prov,
            supersedes_key=supersedes_key,
            is_active=True,
            reader_class=inherited_reader_class,
            created_at_ms=now_ms,
            updated_at_ms=now_ms,
        )
        self._records[key] = record
        return record

    def read(
        self, key: str, active_only: bool = True, reader_class: Optional[str] = None
    ) -> Optional[GovernedMemoryRecord]:
        """Read memory record by key, optionally filtering for active records.

        Authorization is enforced BEFORE returning: a record tagged with a
        ``reader_class`` is only returned when the caller presents the matching
        class. Unrestricted records remain readable by any caller, preserving
        legacy behavior.
        """
        record = self._records.get(key)
        if record is None:
            return None
        if active_only and not record.is_active:
            return None
        if not self._can_read(record, reader_class):
            logger.info(
                "Refusing read of %r: requires reader_class=%r, caller presented %r",
                key,
                record.reader_class,
                reader_class,
            )
            return None
        return record

    def read_with_freshness(
        self, key: str
    ) -> tuple[Optional[GovernedMemoryRecord], FreshnessSignal]:
        """Read a record together with its supersession freshness signal (#3336).

        Unlike :meth:`read` with ``active_only=True`` — which silently hides a
        superseded record — this always returns the record (when it exists) and
        a :class:`FreshnessSignal` telling the caller whether the constraint it
        is about to act on has been withdrawn by a newer authoritative record.
        A superseded record is never silently served as if it were current:
        the caller gets an explicit review signal instead.
        """
        record = self._records.get(key)
        if record is None:
            return None, FreshnessSignal(status="unknown")
        if not record.is_active and record.superseded_by:
            successor = self._records.get(record.superseded_by)
            superseded_at = successor.created_at_ms if successor is not None else None
            return record, FreshnessSignal(
                status="superseded",
                superseded_by=record.superseded_by,
                superseded_at_ms=superseded_at,
            )
        return record, FreshnessSignal(status="current")

    def list_by_scope(
        self, scope: str, active_only: bool = True, reader_class: Optional[str] = None
    ) -> list[GovernedMemoryRecord]:
        """List all memory records belonging to a particular scope.

        Records the caller is not authorized for are excluded — listing must not
        bypass the authorization boundary.
        """
        scope_str = scope.lower()
        results = [
            rec
            for rec in self._records.values()
            if rec.scope == scope_str
            and (not active_only or rec.is_active)
            and self._can_read(rec, reader_class)
        ]
        return results

    def list_by_author(
        self,
        author_id: str,
        active_only: bool = True,
        reader_class: Optional[str] = None,
    ) -> list[GovernedMemoryRecord]:
        """List memory records authored by a specific subagent (authorization enforced)."""
        results = [
            rec
            for rec in self._records.values()
            if rec.provenance.author_subagent_id == author_id
            and (not active_only or rec.is_active)
            and self._can_read(rec, reader_class)
        ]
        return results

    @staticmethod
    def _can_read(record: GovernedMemoryRecord, reader_class: Optional[str]) -> bool:
        """Return True when ``reader_class`` may read ``record`` (fail-closed)."""
        return record.reader_class is None or reader_class == record.reader_class

    def redistribute(
        self,
        superseded_subagent_id: str,
        successor_subagent_id: str,
    ) -> int:
        """Re-home active memory records when a subagent is replaced or superseded."""
        rehomed_count = 0
        now_ms = time.time() * 1000.0

        for record in self._records.values():
            if (
                record.is_active
                and record.provenance.author_subagent_id == superseded_subagent_id
            ):
                # Update authorship while recording original author in provenance sources
                record.provenance.sources.append(
                    f"rehomed_from:{superseded_subagent_id}"
                )
                record.provenance.author_subagent_id = successor_subagent_id
                record.updated_at_ms = now_ms
                rehomed_count += 1

        logger.info(
            "Redistributed %d memory records from %s to %s",
            rehomed_count,
            superseded_subagent_id,
            successor_subagent_id,
        )
        return rehomed_count

    def get_provenance_chain(self, key: str) -> list[GovernedMemoryRecord]:
        """Trace lineage backward through supersedes_key links."""
        chain: list[GovernedMemoryRecord] = []
        curr_key: Optional[str] = key

        visited = set()
        while curr_key and curr_key in self._records and curr_key not in visited:
            visited.add(curr_key)
            rec = self._records[curr_key]
            chain.append(rec)
            curr_key = rec.supersedes_key

        return chain


# Global singleton instance for governed shared memory
_GLOBAL_GOVERNED_MEMORY = GovernedSharedMemory()


def get_global_governed_memory() -> GovernedSharedMemory:
    """Return the global GovernedSharedMemory singleton."""
    return _GLOBAL_GOVERNED_MEMORY
