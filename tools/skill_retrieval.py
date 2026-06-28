"""State-grounded skill retrieval — first increment of online skill acquisition (#247).

Issue #247 ("Online skill acquisition via state-grounded dynamic retrieval")
describes a full system: an in-session online skill pool, an embedding trigger
that surfaces a candidate skill when the current state recurs, write-gating with
similarity dedup + a confidence threshold, and a nightly promotion pathway.

This module ships the smallest coherent, self-contained slice that the rest of
that system builds on: a *state-grounded retrieval helper*. Given the current
task / state text, it ranks the skills already in the registry by relevance and
surfaces the best matches — going beyond the static, alphabetical ``skills_list``
the agent has today. Without this, "retrieve the most relevant skill for the
current state" has no implementation to call; with it, the online pool, the
embedding trigger, and promotion can be layered on later as separate increments.

Deliberately deferred (NOT in this increment):
  - the writable ``skills/online/`` pool and its manifest ``source: online`` mark
  - the post-tool-call embedding trigger that auto-surfaces a recurring skill
  - skill-write gating (embedding dedup ``sim >= 0.9`` + confidence ``>= 0.7``)
  - the nightly evolution job's promotion / review gate
  - any ``sentence-transformers`` / embedding dependency

Scoring is intentionally lexical and dependency-free. The registry has no
embedding index wired in (the ``embedding`` hits elsewhere in the tree are
Unicode-direction characters in the skills security scanner, not vectors), and
#247 itself says to fall back to a small implementation when no embedding
infrastructure is available. Term-overlap over a skill's name / description /
category / tags is a deterministic, well-understood relevance signal that is
easy to test and easy to swap for embeddings in a later increment — the public
``rank_skills`` API is the seam where that swap happens.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence

logger = logging.getLogger(__name__)

# Field weights. Name and tags are the most intentional relevance signals a
# skill author controls; the description is longer and noisier, so a single term
# hit there counts for less. Category is the coarsest signal of all.
_WEIGHT_NAME = 3.0
_WEIGHT_TAGS = 2.5
_WEIGHT_DESCRIPTION = 1.0
_WEIGHT_CATEGORY = 1.5

# A skill the agent has actually used before is a slightly better bet than a
# never-touched one when lexical relevance ties. Kept small so usage can only
# break ties / nudge ordering — it must never float an irrelevant-but-popular
# skill above a relevant one.
_USAGE_BOOST_MAX = 0.5

# Tokens shorter than this are dropped from the query (e.g. "a", "to", "of") so
# stop-word noise doesn't manufacture spurious overlap. Two chars keeps useful
# short identifiers like "ci", "db", "ai".
_MIN_TOKEN_LEN = 2

_TOKEN_RE = re.compile(r"[a-z0-9]+")

# Common English stop words that carry no relevance signal in a task
# description. Kept deliberately small — the goal is to drop obvious noise, not
# to do real linguistics.
_STOP_WORDS = frozenset(
    {
        "the",
        "and",
        "for",
        "with",
        "this",
        "that",
        "from",
        "into",
        "have",
        "has",
        "was",
        "are",
        "you",
        "your",
        "our",
        "can",
        "how",
        "use",
        "using",
        "need",
        "want",
        "please",
        "help",
        "make",
        "get",
        "set",
        "via",
        "out",
        "now",
        "all",
        "any",
        "some",
        "what",
        "when",
        "where",
        "which",
        # Common two-letter function words — pure noise, no relevance signal.
        "to",
        "of",
        "in",
        "on",
        "is",
        "it",
        "be",
        "by",
        "or",
        "as",
        "at",
        "an",
        "we",
        "do",
        "if",
        "so",
        "up",
        "my",
        "me",
    }
)


@dataclass(frozen=True)
class RankedSkill:
    """A single ranked retrieval result.

    ``score`` is a non-negative relevance number (higher is more relevant); it is
    not normalised to any fixed range because callers only ever compare scores
    against each other within one query, never across queries.
    """

    name: str
    description: str
    category: Optional[str]
    score: float
    matched_terms: List[str]


def _tokenize(text: str) -> List[str]:
    """Lowercase, split on non-alphanumerics, drop stop words and tiny tokens."""
    if not text:
        return []
    tokens = _TOKEN_RE.findall(text.lower())
    return [
        t for t in tokens if len(t) >= _MIN_TOKEN_LEN and t not in _STOP_WORDS
    ]


def _field_overlap(
    query_terms: set, field_text: str, weight: float
) -> tuple[float, set]:
    """Score one skill field against the query.

    Returns ``(weighted_score, matched_query_terms)``. Each distinct query term
    that appears in the field contributes ``weight`` once — repeated occurrences
    don't compound, so a description that spams a keyword can't out-rank a
    genuine name match.
    """
    field_terms = set(_tokenize(field_text))
    if not field_terms:
        return 0.0, set()
    matched = query_terms & field_terms
    return len(matched) * weight, matched


def _usage_boost(name: str, usage: Dict[str, Dict[str, Any]]) -> float:
    """Small additive boost in ``[0, _USAGE_BOOST_MAX]`` from prior usage.

    Uses the combined use+view+patch activity count, squashed through a
    saturating curve so the first few uses matter and heavy use can't dominate
    lexical relevance. Best-effort: any malformed record yields no boost.
    """
    record = usage.get(name)
    if not isinstance(record, dict):
        return 0.0
    try:
        from tools.skill_usage import activity_count
    except Exception:  # pragma: no cover - skill_usage always importable in tree
        return 0.0
    count = activity_count(record)
    if count <= 0:
        return 0.0
    # 1 use -> 0.5*MAX, 3 -> 0.75*MAX, saturating toward MAX. count/(count+1)
    # is a cheap saturating curve that never reaches the ceiling.
    return _USAGE_BOOST_MAX * (count / (count + 1.0))


def _candidate_text_fields(candidate: Dict[str, Any]) -> Dict[str, str]:
    """Pull the scorable text out of a candidate dict, tolerant of shape.

    Tags may arrive as a list (from frontmatter) or be absent; everything else
    is coerced to a string. ``_find_all_skills`` does not currently surface
    tags, so this helper also reads a ``tags`` key when a richer candidate
    source provides one.
    """
    tags_value = candidate.get("tags")
    if isinstance(tags_value, (list, tuple)):
        tags_text = " ".join(str(t) for t in tags_value)
    else:
        tags_text = str(tags_value or "")
    return {
        "name": str(candidate.get("name") or ""),
        "description": str(candidate.get("description") or ""),
        "category": str(candidate.get("category") or ""),
        "tags": tags_text,
    }


def rank_skills(
    query: str,
    *,
    limit: int = 5,
    candidates: Optional[Sequence[Dict[str, Any]]] = None,
    use_usage_signal: bool = True,
) -> List[RankedSkill]:
    """Rank registry skills by relevance to the current task / state *query*.

    This is the state-grounded retrieval seam from #247: the caller passes the
    text that describes "where the agent is right now" (the task, the last few
    turns, a tool result summary — anything) and gets back the skills most
    likely to help, best-first.

    Args:
        query: Free-form text describing the current task or state.
        limit: Maximum number of results to return (results with a zero
            relevance score are always dropped, so fewer may come back).
        candidates: Optional explicit list of skill metadata dicts (each with at
            least ``name``; ``description``/``category``/``tags`` optional). When
            omitted, the live registry is enumerated via
            ``skills_tool._find_all_skills`` and enriched with frontmatter tags.
        use_usage_signal: When True (default), apply a small prior-usage boost so
            proven skills win ties. Disable for fully deterministic, usage-free
            ranking (used by tests and by callers that want pure lexical order).

    Returns:
        A list of ``RankedSkill`` ordered by descending score, then by name for
        a stable tie-break. Empty when the query has no scorable terms or no
        candidate matches it.
    """
    query_terms = set(_tokenize(query))
    if not query_terms:
        return []

    if candidates is None:
        candidates = _load_candidates_from_registry()

    usage: Dict[str, Dict[str, Any]] = {}
    if use_usage_signal:
        try:
            from tools.skill_usage import load_usage

            usage = load_usage()
        except Exception:  # pragma: no cover - best-effort telemetry read
            logger.debug("Could not load skill usage for ranking", exc_info=True)
            usage = {}

    ranked: List[RankedSkill] = []
    for candidate in candidates:
        name = str(candidate.get("name") or "").strip()
        if not name:
            continue
        fields = _candidate_text_fields(candidate)

        score = 0.0
        matched: set = set()
        for field_name, weight in (
            ("name", _WEIGHT_NAME),
            ("tags", _WEIGHT_TAGS),
            ("category", _WEIGHT_CATEGORY),
            ("description", _WEIGHT_DESCRIPTION),
        ):
            field_score, field_matched = _field_overlap(
                query_terms, fields[field_name], weight
            )
            score += field_score
            matched |= field_matched

        if score <= 0.0:
            # No lexical relevance — a usage boost alone must never surface a
            # skill the current state has nothing to do with.
            continue

        if use_usage_signal:
            score += _usage_boost(name, usage)

        ranked.append(
            RankedSkill(
                name=name,
                description=fields["description"],
                category=candidate.get("category"),
                score=round(score, 4),
                matched_terms=sorted(matched),
            )
        )

    ranked.sort(key=lambda r: (-r.score, r.name))
    if limit is not None and limit >= 0:
        ranked = ranked[:limit]
    return ranked


def _load_candidates_from_registry() -> List[Dict[str, Any]]:
    """Enumerate live skills and enrich each with frontmatter tags.

    ``skills_tool._find_all_skills`` already does the platform/environment/
    disabled filtering and dedup we want, but returns only name/description/
    category. Tags are a strong relevance signal, so we re-read each skill's
    frontmatter to attach them. Tag enrichment is best-effort: a skill whose
    frontmatter can't be read still ranks on its name/description/category.
    """
    from tools import skills_tool
    from agent.skill_utils import iter_skill_index_files, parse_frontmatter

    skills = skills_tool._find_all_skills()

    # Build a name -> tags map by scanning the same dirs _find_all_skills uses.
    tags_by_name: Dict[str, List[str]] = {}
    try:
        from agent.skill_utils import get_external_skills_dirs

        scan_dirs = []
        if skills_tool.SKILLS_DIR.exists():
            scan_dirs.append(skills_tool.SKILLS_DIR)
        scan_dirs.extend(get_external_skills_dirs())

        for scan_dir in scan_dirs:
            for skill_md in iter_skill_index_files(scan_dir, "SKILL.md"):
                try:
                    content = skill_md.read_text(encoding="utf-8")[:4000]
                    frontmatter, _ = parse_frontmatter(content)
                except Exception:
                    continue
                name = str(frontmatter.get("name") or skill_md.parent.name)
                if name in tags_by_name:
                    continue
                tags_by_name[name] = _extract_tags(frontmatter)
    except Exception:  # pragma: no cover - enrichment is best-effort
        logger.debug("Skill tag enrichment failed", exc_info=True)

    for skill in skills:
        tags = tags_by_name.get(skill.get("name", ""))
        if tags:
            skill["tags"] = tags
    return skills


def _extract_tags(frontmatter: Dict[str, Any]) -> List[str]:
    """Read tags from frontmatter, honouring the metadata.hermes.* convention.

    Mirrors the lookup ``skills_tool.skill_view`` does: ``metadata.hermes.tags``
    first (agentskills.io convention), then a top-level ``tags`` fallback.
    """
    metadata = frontmatter.get("metadata")
    hermes_meta: Dict[str, Any] = {}
    if isinstance(metadata, dict):
        candidate = metadata.get("hermes")
        if isinstance(candidate, dict):
            hermes_meta = candidate

    raw = hermes_meta.get("tags")
    if raw is None:
        raw = frontmatter.get("tags")
    if not raw:
        return []
    if isinstance(raw, (list, tuple)):
        return [str(t).strip() for t in raw if str(t).strip()]
    # Comma / bracket separated string fallback.
    text = str(raw).strip()
    if text.startswith("[") and text.endswith("]"):
        text = text[1:-1]
    return [t.strip().strip("\"'") for t in text.split(",") if t.strip()]
