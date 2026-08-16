# -*- coding: utf-8 -*-
"""Governed shared memory with scope, provenance, supersession, and redistribution (Issue #2488, Slice B, MemClaw).

Mitigates multi-agent shared memory failure modes:
1. Leakage (strict scope boundaries)
2. Stale propagation (temporal supersession tracking)
3. Contradiction persistence (explicit supersedes relations)
4. Provenance collapse (full author/tool provenance tracing)
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
    sources: List[str] = field(default_factory=list)
    timestamp_ms: float = field(default_factory=lambda: time.time() * 1000.0)
    confidence: float = 1.0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


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
    created_at_ms: float = field(default_factory=lambda: time.time() * 1000.0)
    updated_at_ms: float = field(default_factory=lambda: time.time() * 1000.0)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class GovernedSharedMemory:
    """Central store managing governed memory records with provenance and redistribution."""

    def __init__(self) -> None:
        self._records: Dict[str, GovernedMemoryRecord] = {}

    def write(
        self,
        key: str,
        value: Any,
        author_id: str,
        scope: str = MemoryScope.TASK.value,
        source_tool: str = "",
        sources: Optional[List[str]] = None,
        supersedes_key: Optional[str] = None,
        confidence: float = 1.0,
    ) -> GovernedMemoryRecord:
        """Write a new governed memory record with provenance and handle supersession."""
        now_ms = time.time() * 1000.0
        prov = MemoryProvenance(
            author_subagent_id=author_id,
            source_tool=source_tool,
            sources=sources or [],
            timestamp_ms=now_ms,
            confidence=confidence,
        )

        # Handle supersession of existing record
        if supersedes_key and supersedes_key in self._records:
            old_record = self._records[supersedes_key]
            old_record.is_active = False
            old_record.superseded_by = key
            old_record.updated_at_ms = now_ms

        record = GovernedMemoryRecord(
            key=key,
            value=value,
            scope=scope.lower() if isinstance(scope, str) else MemoryScope.TASK.value,
            provenance=prov,
            supersedes_key=supersedes_key,
            is_active=True,
            created_at_ms=now_ms,
            updated_at_ms=now_ms,
        )
        self._records[key] = record
        return record

    def read(
        self, key: str, active_only: bool = True
    ) -> Optional[GovernedMemoryRecord]:
        """Read memory record by key, optionally filtering for active records."""
        record = self._records.get(key)
        if record is None:
            return None
        if active_only and not record.is_active:
            return None
        return record

    def list_by_scope(
        self, scope: str, active_only: bool = True
    ) -> List[GovernedMemoryRecord]:
        """List all memory records belonging to a particular scope."""
        scope_str = scope.lower()
        results = [
            rec
            for rec in self._records.values()
            if rec.scope == scope_str and (not active_only or rec.is_active)
        ]
        return results

    def list_by_author(
        self, author_id: str, active_only: bool = True
    ) -> List[GovernedMemoryRecord]:
        """List memory records authored by a specific subagent."""
        results = [
            rec
            for rec in self._records.values()
            if rec.provenance.author_subagent_id == author_id
            and (not active_only or rec.is_active)
        ]
        return results

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

    def get_provenance_chain(self, key: str) -> List[GovernedMemoryRecord]:
        """Trace lineage backward through supersedes_key links."""
        chain: List[GovernedMemoryRecord] = []
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
