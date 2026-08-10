"""Deterministic skill consolidation — embedding-free clustering by description similarity.

Complements the curator's LLM umbrella-building pass by pre-clustering similar
skills BEFORE the expensive LLM fork. The LLM pass then receives clusters as
hints, reducing the candidate search space and making consolidation tractable
for large skill libraries.

Inspired by EvoLib (arXiv:2605.14477) — consolidation across task types is what
makes accumulated knowledge beneficial, as opposed to raw-trajectory retrieval
which is net-negative on heterogeneous tasks.

Design:
  - No external embeddings (avoids a model dependency). Uses token-overlap
    Jaccard similarity on skill descriptions + tags, which is cheap, deterministic,
    and sufficient for grouping obviously-similar skills.
  - Returns cluster suggestions, never mutates skills directly. The LLM pass
    (or a human) decides whether to merge.
  - Wired into ``run_curator_review`` so it runs on every enabled curator pass.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Set, Tuple

from tools import skill_usage

logger = logging.getLogger(__name__)

# Minimum Jaccard similarity for two skills to be in the same cluster.
_SIMILARITY_THRESHOLD = 0.35
# Minimum cluster size — singletons are not useful consolidation candidates.
_MIN_CLUSTER_SIZE = 2


def _tokenize(text: Optional[str]) -> Set[str]:
    """Split text into a lowercase token set, dropping short/common words."""
    if not text:
        return set()
    # Simple split on non-alphanumeric — good enough for description overlap.
    tokens = set()
    for word in text.lower().replace("-", " ").replace("_", " ").split():
        # Keep tokens >= 3 chars, drop a small stopword set.
        clean = "".join(c for c in word if c.isalnum())
        if len(clean) >= 3 and clean not in _STOPWORDS:
            tokens.add(clean)
    return tokens


_STOPWORDS = frozenset({
    "the",
    "and",
    "for",
    "are",
    "but",
    "not",
    "you",
    "all",
    "any",
    "can",
    "had",
    "her",
    "was",
    "one",
    "our",
    "out",
    "has",
    "have",
    "from",
    "this",
    "that",
    "with",
    "will",
    "your",
    "tool",
    "skill",
    "agent",
    "hermes",
    "using",
    "used",
    "use",
    "when",
    "what",
    "how",
    "which",
    "into",
    "they",
})


def _jaccard(a: Set[str], b: Set[str]) -> float:
    """Jaccard similarity between two token sets."""
    if not a or not b:
        return 0.0
    intersection = a & b
    union = a | b
    return len(intersection) / len(union) if union else 0.0


def _build_skill_tokens(rows: List[Dict[str, Any]]) -> Dict[str, Set[str]]:
    """Extract a token set for each skill from its description + tags."""
    # Lazy import to avoid circular dependency at module load.
    from tools.skills_tool import _find_all_skills

    # Build a name → metadata lookup from the full skill scan.
    meta_by_name: Dict[str, Dict[str, Any]] = {}
    try:
        for skill_meta in _find_all_skills():
            if isinstance(skill_meta, dict):
                n = skill_meta.get("name", "")
                if n:
                    meta_by_name[n] = skill_meta
    except Exception:
        pass

    result: Dict[str, Set[str]] = {}
    for row in rows:
        name = row.get("name", "")
        if not name:
            continue
        desc = ""
        tags = ""
        skill_meta = meta_by_name.get(name)
        if isinstance(skill_meta, dict):
            desc = str(skill_meta.get("description", ""))
            meta = skill_meta.get("metadata", {})
            if isinstance(meta, dict):
                hermes_meta = meta.get("hermes", {})
                if isinstance(hermes_meta, dict):
                    tag_list = hermes_meta.get("tags", [])
                    if isinstance(tag_list, list):
                        tags = " ".join(str(t) for t in tag_list)
        tokens = _tokenize(desc) | _tokenize(tags)
        tokens |= _tokenize(name)
        result[name] = tokens
    return result


def _cluster(
    names: List[str], tokens: Dict[str, Set[str]], threshold: float
) -> List[List[str]]:
    """Group names into clusters by greedy single-linkage Jaccard similarity."""
    visited: Set[str] = set()
    clusters: List[List[str]] = []
    for name in names:
        if name in visited:
            continue
        cluster = [name]
        visited.add(name)
        # Greedy: find all unvisited names similar to any member.
        changed = True
        while changed:
            changed = False
            for other in names:
                if other in visited:
                    continue
                for member in cluster:
                    sim = _jaccard(tokens.get(member, set()), tokens.get(other, set()))
                    if sim >= threshold:
                        cluster.append(other)
                        visited.add(other)
                        changed = True
                        break
        if len(cluster) >= _MIN_CLUSTER_SIZE:
            clusters.append(cluster)
    return clusters


def run_consolidation_pass(
    threshold: float = _SIMILARITY_THRESHOLD,
) -> Dict[str, Any]:
    """Run a deterministic consolidation pass over curator-managed skills.

    Clusters similar skills by description/tag overlap and returns cluster
    suggestions. Does NOT mutate skills — the LLM pass or a human decides
    whether to merge.

    Returns a dict with:
      - ``clusters``: list of cluster dicts, each with ``members`` and
        ``suggested_umbrella`` (the member with the highest activity).
      - ``total_skills``: number of curated skills examined.
      - ``clustered_skills``: number of skills in a cluster of size >= 2.
    """
    try:
        rows = skill_usage.curated_report()
    except Exception as e:
        logger.debug("consolidation: curated_report() failed: %s", e)
        return {"clusters": [], "total_skills": 0, "clustered_skills": 0}

    # Only cluster active skills — stale/archived ones are not useful candidates.
    active_rows = [r for r in rows if r.get("state", "active") == "active"]
    if len(active_rows) < _MIN_CLUSTER_SIZE:
        return {
            "clusters": [],
            "total_skills": len(rows),
            "clustered_skills": 0,
        }

    names = [r.get("name", "") for r in active_rows if r.get("name")]
    tokens = _build_skill_tokens(active_rows)
    raw_clusters = _cluster(names, tokens, threshold)

    # Build cluster dicts with a suggested umbrella (highest-activity member).
    activity_by_name = {
        r.get("name", ""): r.get("activity_count", 0) for r in active_rows
    }
    clusters: List[Dict[str, Any]] = []
    for members in raw_clusters:
        umbrella = max(members, key=lambda n: activity_by_name.get(n, 0))
        clusters.append({
            "members": sorted(members),
            "suggested_umbrella": umbrella,
            "member_count": len(members),
        })

    clustered_count = sum(c["member_count"] for c in clusters)
    return {
        "clusters": clusters,
        "total_skills": len(rows),
        "clustered_skills": clustered_count,
    }


def render_clusters_for_prompt(result: Dict[str, Any]) -> str:
    """Render cluster suggestions as a text block for the LLM review prompt."""
    clusters = result.get("clusters", [])
    if not clusters:
        return ""
    lines = ["Pre-clustered similarity groups (deterministic — review for merge):"]
    for i, c in enumerate(clusters, 1):
        members = ", ".join(c.get("members", []))
        umbrella = c.get("suggested_umbrella", "")
        lines.append(f"  Cluster {i}: [{members}] — suggested umbrella: {umbrella}")
    return "\n".join(lines)
