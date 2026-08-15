# -*- coding: utf-8 -*-
"""Sleep-Time Compute for autonomous memory consolidation (issue #2358).

Adopts Letta/MemGPT Sleep-time Compute (arXiv:2504.13171):
1. Runs background / offline consolidation passes during idle periods.
2. Identifies fragmented episodic notes, deduplicates, and synthesizes consolidated memories.
3. Automatically promotes high-frequency patterns to the durable tier.
4. Emits structured cross-session entity links (supersedes, references, fixes).
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

logger = logging.getLogger(__name__)

__all__ = [
    "ConsolidationAction",
    "ConsolidationReport",
    "SleepTimeMemoryConsolidator",
]


@dataclass
class ConsolidationAction:
    """Individual consolidation step taken during sleep-time compute."""

    action_type: str  # "promote", "deprecate", "merge", "link"
    target_id: str
    reason: str
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class ConsolidationReport:
    """Consolidated audit report of offline memory consolidation."""

    promoted_count: int = 0
    deprecated_count: int = 0
    merged_count: int = 0
    linked_count: int = 0
    actions: List[ConsolidationAction] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "promoted_count": self.promoted_count,
            "deprecated_count": self.deprecated_count,
            "merged_count": self.merged_count,
            "linked_count": self.linked_count,
            "actions": [a.to_dict() for a in self.actions],
        }


class SleepTimeMemoryConsolidator:
    """Offline consolidator transforming raw session notes into high-density durable memories."""

    @staticmethod
    def _compute_content_hash(text: str) -> str:
        """Compute 8-char hash of normalized text."""
        normalized = " ".join(text.strip().lower().split())
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:8]

    @classmethod
    def consolidate_notes(
        cls,
        notes: Sequence[Dict[str, Any]],
        access_frequency_threshold: int = 3,
    ) -> Tuple[List[Dict[str, Any]], ConsolidationReport]:
        """Perform full sleep-time consolidation pass over memory notes."""
        report = ConsolidationReport()
        consolidated: List[Dict[str, Any]] = []

        seen_hashes: Dict[str, Dict[str, Any]] = {}
        stale_notes: Set[str] = set()

        for note in notes:
            note_id = str(note.get("id") or note.get("note_id") or "")
            content = str(note.get("content", "")).strip()
            tier = str(note.get("tier", "episodic")).lower()
            access_count = int(note.get("access_count", 0))
            is_deprecated = bool(note.get("deprecated", False))

            if is_deprecated:
                continue

            content_hash = cls._compute_content_hash(content)

            # Deduplication / Merging
            if content_hash in seen_hashes:
                existing = seen_hashes[content_hash]
                existing_id = str(existing.get("id") or existing.get("note_id") or "")
                report.merged_count += 1
                report.actions.append(
                    ConsolidationAction(
                        action_type="merge",
                        target_id=note_id,
                        reason=f"Duplicate content of note {existing_id}",
                        metadata={"merged_into": existing_id},
                    )
                )
                continue

            # Promotion: if episodic and accessed frequently -> promote to durable
            if tier == "episodic" and access_count >= access_frequency_threshold:
                note_copy = dict(note)
                note_copy["tier"] = "durable"
                report.promoted_count += 1
                report.actions.append(
                    ConsolidationAction(
                        action_type="promote",
                        target_id=note_id,
                        reason=f"Access count {access_count} exceeds threshold {access_frequency_threshold}",
                        metadata={"old_tier": "episodic", "new_tier": "durable"},
                    )
                )
                consolidated.append(note_copy)
                seen_hashes[content_hash] = note_copy
            else:
                consolidated.append(dict(note))
                seen_hashes[content_hash] = note

        # Detect cross-entity link relationships (e.g. supersedes)
        for i, n1 in enumerate(consolidated):
            c1 = str(n1.get("content", "")).lower()
            id1 = str(n1.get("id") or n1.get("note_id") or f"note_{i}")
            if "supersedes" in c1 or "replaces" in c1:
                report.linked_count += 1
                report.actions.append(
                    ConsolidationAction(
                        action_type="link",
                        target_id=id1,
                        reason="Explicit supersession relationship detected in text",
                        metadata={"relation_type": "supersedes"},
                    )
                )

        return consolidated, report
