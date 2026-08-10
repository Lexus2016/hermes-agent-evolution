"""Knowledge consolidation pass — merge similar skills/insights (#2184).

EvoLib (arXiv:2605.14477, Microsoft) shows raw-trajectory retrieval HURTS
on heterogeneous tasks (ExpRAG: −23.4pp PDDL), while consolidation across
task types gives +20.4% HMMT, +11.1% BigCodeBench. Hermes stores raw notes
+ skills but lacks the consolidation/weighting step that converts accumulated
experience into progressively more general, reusable knowledge.

This module adds a **consolidation pass** (distinct from the dreaming pass):
  1. Every N cycles: cluster similar skill/memory entries by token-overlap
     similarity (same lightweight approach as skill_retrieval — no embedding
     dependency required).
  2. For each cluster: identify a consolidation candidate (the highest-utility
     entry that could subsume the others).
  3. Weight each entry by recent-utility and age.
  4. Report clusters + recommendations for the curator to act on.

This pass is **advisory** — it does not auto-merge or delete skills. The
curator (or the dreaming pass) consumes the recommendations.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)

# Similarity threshold for clustering (token Jaccard index)
_CLUSTER_THRESHOLD = 0.35
_MIN_CLUSTER_SIZE = 2
# Utility weight: how much recent activity matters vs age
_UTILITY_WEIGHT = 0.7
_AGE_DECAY_DAYS = 30.0

_TOKEN_RE = re.compile(r"[a-z0-9]+")
_STOP_WORDS = frozenset({
    "the",
    "a",
    "an",
    "to",
    "is",
    "are",
    "for",
    "in",
    "on",
    "of",
    "and",
    "or",
    "with",
    "this",
    "that",
    "it",
    "be",
    "will",
    "you",
    "your",
    "skill",
    "use",
    "using",
    "when",
    "how",
    "what",
    "from",
    "by",
})


@dataclass
class SkillEntry:
    """A skill entry in the consolidation pool."""

    name: str
    description: str
    category: Optional[str] = None
    invocation_count: int = 0
    failure_rate: float = 0.0
    created_at: Optional[str] = None
    last_used_at: Optional[str] = None

    @property
    def text(self) -> str:
        return f"{self.name} {self.description}".strip()


@dataclass
class ClusterResult:
    """A cluster of similar skills + consolidation recommendation."""

    members: List[str] = field(default_factory=list)
    consolidation_candidate: Optional[str] = None
    avg_similarity: float = 0.0
    demotion_candidates: List[str] = field(default_factory=list)
    reason: str = ""


# -- Tokenization + similarity ---------------------------------------------


def _tokenize(text: str) -> Set[str]:
    if not text:
        return set()
    tokens = _TOKEN_RE.findall(text.lower())
    return {t for t in tokens if len(t) >= 2 and t not in _STOP_WORDS}


def _jaccard(a: Set[str], b: Set[str]) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    union = len(a | b)
    return inter / union if union else 0.0


def skill_similarity(a: SkillEntry, b: SkillEntry) -> float:
    """Token-overlap similarity between two skills (Jaccard index)."""
    return _jaccard(_tokenize(a.text), _tokenize(b.text))


# -- Utility + age weighting -----------------------------------------------


def entry_weight(entry: SkillEntry, now: Optional[datetime] = None) -> float:
    """Compute the utility+age weight for an entry.

    Higher = more valuable to keep. Combines invocation activity (utility)
    with age decay — recently-active skills weigh more.

    Returns a float in [0, ~1.5] range (not normalized).
    """
    if now is None:
        now = datetime.now(timezone.utc)

    # Utility: sqrt(invocation_count) * (1 - failure_rate)
    utility = (entry.invocation_count**0.5) * max(0.0, 1.0 - entry.failure_rate)

    # Age decay: skills not used recently get discounted
    age_factor = 1.0
    if entry.last_used_at:
        try:
            last = datetime.fromisoformat(entry.last_used_at.replace("Z", "+00:00"))
            days_since = (now - last).days
            age_factor = max(0.1, 1.0 - days_since / _AGE_DECAY_DAYS)
        except Exception:
            pass

    return _UTILITY_WEIGHT * utility + (1 - _UTILITY_WEIGHT) * age_factor


# -- Clustering ------------------------------------------------------------


def cluster_skills(
    entries: List[SkillEntry],
    threshold: float = _CLUSTER_THRESHOLD,
) -> List[ClusterResult]:
    """Cluster similar skills by token-overlap similarity.

    Uses a simple greedy single-linkage approach: for each unassigned entry,
    find all others above the threshold and group them.

    Args:
        entries: the skill pool to cluster
        threshold: minimum Jaccard similarity to cluster together

    Returns clusters with ≥ _MIN_CLUSTER_SIZE members.
    """
    if len(entries) < _MIN_CLUSTER_SIZE:
        return []

    # Pre-compute token sets
    tokens = {e.name: _tokenize(e.text) for e in entries}
    entry_map = {e.name: e for e in entries}

    assigned: Set[str] = set()
    clusters: List[ClusterResult] = []

    for entry in entries:
        if entry.name in assigned:
            continue
        # Find similar unassigned entries
        members = [entry.name]
        sims = []
        assigned.add(entry.name)
        for other in entries:
            if other.name in assigned:
                continue
            sim = _jaccard(tokens[entry.name], tokens[other.name])
            if sim >= threshold:
                members.append(other.name)
                sims.append(sim)
                assigned.add(other.name)

        if len(members) >= _MIN_CLUSTER_SIZE:
            avg_sim = sum(sims) / len(sims) if sims else 0.0
            clusters.append(
                ClusterResult(
                    members=members,
                    avg_similarity=round(avg_sim, 4),
                )
            )

    return clusters


def recommend_consolidation(
    cluster: ClusterResult,
    entries: List[SkillEntry],
    now: Optional[datetime] = None,
) -> ClusterResult:
    """Pick the consolidation candidate and demotion targets for a cluster.

    The highest-weight entry becomes the consolidation candidate (umbrella).
    Lower-weight members are demotion candidates.
    """
    entry_map = {e.name: e for e in entries}
    weighted = []
    for name in cluster.members:
        e = entry_map.get(name)
        if e:
            w = entry_weight(e, now)
            weighted.append((name, w))

    if not weighted:
        return cluster

    weighted.sort(key=lambda x: x[1], reverse=True)
    cluster.consolidation_candidate = weighted[0][0]

    # Members with weight < 50% of the top entry's weight are demotion targets
    top_weight = weighted[0][1]
    threshold = top_weight * 0.5
    cluster.demotion_candidates = [name for name, w in weighted[1:] if w < threshold]
    cluster.reason = (
        f"Highest utility: {weighted[0][0]} (w={top_weight:.2f}). "
        f"{len(cluster.demotion_candidates)} lower-utility members "
        f"available for merge/demotion."
    )
    return cluster


# -- Main entry point ------------------------------------------------------


def run_consolidation_pass(
    entries: List[SkillEntry],
    threshold: float = _CLUSTER_THRESHOLD,
    now: Optional[datetime] = None,
) -> List[ClusterResult]:
    """Run a full consolidation pass over the skill pool.

    Returns a list of clusters with consolidation recommendations. The
    curator or dreaming pass consumes these to merge/demote entries.
    """
    clusters = cluster_skills(entries, threshold)
    return [recommend_consolidation(c, entries, now) for c in clusters]
