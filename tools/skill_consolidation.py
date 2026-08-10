"""Deterministic skill consolidation — embedding-free clustering by similarity.

Pre-clusters similar skills BEFORE the LLM umbrella-building pass. Inspired by
EvoLib (arXiv:2605.14477). Uses token-overlap Jaccard; never mutates skills.
Wired into run_curator_review.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Set

from tools import skill_usage

logger = logging.getLogger(__name__)

_SIMILARITY_THRESHOLD = 0.35
_MIN_CLUSTER_SIZE = 2

_STOPWORDS = frozenset(
    "the and for are but not you all any can had her was one our out has have "
    "from this that with will your tool skill agent hermes using used use when "
    "what how which into they".split()
)


def _tokenize(text: Optional[str]) -> Set[str]:
    """Lowercase token set, dropping short/common words."""
    if not text:
        return set()
    tokens = set()
    for word in text.lower().replace("-", " ").replace("_", " ").split():
        clean = "".join(c for c in word if c.isalnum())
        if len(clean) >= 3 and clean not in _STOPWORDS:
            tokens.add(clean)
    return tokens


def _jaccard(a: Set[str], b: Set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _build_skill_tokens(rows: List[Dict[str, Any]]) -> Dict[str, Set[str]]:
    """Token set per skill from description + tags."""
    from tools.skills_tool import _find_all_skills

    meta_by_name: Dict[str, Dict[str, Any]] = {}
    try:
        for sm in _find_all_skills():
            if isinstance(sm, dict) and sm.get("name"):
                meta_by_name[sm["name"]] = sm
    except Exception:
        pass

    result: Dict[str, Set[str]] = {}
    for row in rows:
        name = row.get("name", "")
        if not name:
            continue
        desc, tags = "", ""
        sm = meta_by_name.get(name)
        if isinstance(sm, dict):
            desc = str(sm.get("description", ""))
            meta = sm.get("metadata", {})
            if isinstance(meta, dict):
                hm = meta.get("hermes", {})
                tl = hm.get("tags", []) if isinstance(hm, dict) else []
                if isinstance(tl, list):
                    tags = " ".join(str(t) for t in tl)
        result[name] = _tokenize(desc) | _tokenize(tags) | _tokenize(name)
    return result


def _cluster(names: List[str], tokens: Dict[str, Set[str]], threshold: float) -> List[List[str]]:
    """Greedy single-linkage Jaccard clustering."""
    visited: Set[str] = set()
    clusters: List[List[str]] = []
    for name in names:
        if name in visited:
            continue
        cluster = [name]
        visited.add(name)
        changed = True
        while changed:
            changed = False
            for other in names:
                if other in visited:
                    continue
                for member in cluster:
                    if _jaccard(tokens.get(member, set()), tokens.get(other, set())) >= threshold:
                        cluster.append(other)
                        visited.add(other)
                        changed = True
                        break
        if len(cluster) >= _MIN_CLUSTER_SIZE:
            clusters.append(cluster)
    return clusters


def run_consolidation_pass(threshold: float = _SIMILARITY_THRESHOLD) -> Dict[str, Any]:
    """Cluster curator-managed skills by similarity. Returns cluster suggestions
    (members + suggested umbrella). Never mutates skills."""
    try:
        rows = skill_usage.curated_report()
    except Exception as e:
        logger.debug("consolidation: curated_report() failed: %s", e)
        return {"clusters": [], "total_skills": 0, "clustered_skills": 0}

    active = [r for r in rows if r.get("state", "active") == "active"]
    if len(active) < _MIN_CLUSTER_SIZE:
        return {"clusters": [], "total_skills": len(rows), "clustered_skills": 0}

    names = [r.get("name", "") for r in active if r.get("name")]
    raw = _cluster(names, _build_skill_tokens(active), threshold)
    activity = {r.get("name", ""): r.get("activity_count", 0) for r in active}
    clusters = [
        {"members": sorted(m), "suggested_umbrella": max(m, key=lambda n: activity.get(n, 0)), "member_count": len(m)}
        for m in raw
    ]
    return {"clusters": clusters, "total_skills": len(rows), "clustered_skills": sum(c["member_count"] for c in clusters)}


def render_clusters_for_prompt(result: Dict[str, Any]) -> str:
    """Render clusters as text for the LLM review prompt."""
    clusters = result.get("clusters", [])
    if not clusters:
        return ""
    lines = ["Pre-clustered similarity groups (deterministic — review for merge):"]
    for i, c in enumerate(clusters, 1):
        lines.append(f"  Cluster {i}: [{', '.join(c.get('members', []))}] — suggested umbrella: {c.get('suggested_umbrella', '')}")
    return "\n".join(lines)