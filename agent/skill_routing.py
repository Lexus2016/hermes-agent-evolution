# -*- coding: utf-8 -*-
"""Compositional skill routing: SAD re-decomposition + listwise reranking (#1137).

Adopts the two empirically-validated mechanisms from Compositional Skill Routing
(arXiv:2606.18051) without the unvalidated DAG compose stage:

* **Skill-Aware Decomposition (SAD)** — :func:`sad_redecompose` feeds the
  candidate skill/tool names + descriptions returned by the first retrieval pass
  back into a re-decomposition step, anchoring sub-task generation on the real
  vocabulary. LLM-driven (injectable ``llm_call``) so it is testable without a
  live model.
* **Listwise reranking** — :func:`listwise_rerank` reorders the top-k
  ``tool_search`` candidates by a listwise relevance signal and returns them
  best-first. The scorer is pluggable: the deterministic default rewards
  name-token coverage + query/description overlap (a signal BM25's per-term
  scoring does not directly capture); an LLM scorer can be injected in
  production.

Both mechanisms are **config-gated and off by default** (:func:`load_skill_routing_config`).
The runtime seam :func:`maybe_rerank_hits` returns the hits unchanged unless
listwise reranking is enabled, so ``tool_search`` behaves identically by default.

Design goals: pure functions + frozen dataclasses; no import-time side effects;
standard library only; duck-typed candidates (anything with ``.name`` and
``.description``) so this module never imports ``tools.tool_search`` (avoids an
import cycle).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Sequence

__all__ = [
    "SkillRoutingConfig",
    "load_skill_routing_config",
    "listwise_rerank",
    "sad_redecompose",
    "maybe_rerank_hits",
    "lexical_listwise_score",
]

_WORD_RE = re.compile(r"[a-z0-9]+")


@dataclass(frozen=True)
class SkillRoutingConfig:
    """Config for the ``skill_routing`` section (both levers off by default)."""

    sad_enabled: bool = False
    listwise_rerank_enabled: bool = False
    rerank_top_k: int = 10

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any] | None) -> "SkillRoutingConfig":
        if not isinstance(data, Mapping):
            return cls()
        defaults = cls()
        try:
            top_k = int(data.get("rerank_top_k", defaults.rerank_top_k))
        except (TypeError, ValueError):
            top_k = defaults.rerank_top_k
        return cls(
            sad_enabled=bool(data.get("sad", data.get("sad_enabled", defaults.sad_enabled))),
            listwise_rerank_enabled=bool(
                data.get("listwise_rerank", data.get("listwise_rerank_enabled", defaults.listwise_rerank_enabled))
            ),
            rerank_top_k=max(1, top_k),
        )


def load_skill_routing_config() -> SkillRoutingConfig:
    """Lazily load the ``skill_routing`` config section (safe default: all off)."""
    try:
        from hermes_cli.config import load_config

        cfg = load_config()
        if isinstance(cfg, Mapping):
            return SkillRoutingConfig.from_mapping(cfg.get("skill_routing", {}))
    except Exception:
        pass
    return SkillRoutingConfig()


def _tokens(text: str) -> list[str]:
    return _WORD_RE.findall((text or "").lower())


def _candidate_text(candidate: Any) -> tuple[str, str]:
    """Return ``(name, description)`` for a duck-typed candidate."""
    if isinstance(candidate, Mapping):
        return str(candidate.get("name", "")), str(candidate.get("description", ""))
    return str(getattr(candidate, "name", "")), str(getattr(candidate, "description", ""))


def lexical_listwise_score(query: str, candidate: Any) -> float:
    """Deterministic listwise relevance score for one candidate.

    Combines two signals BM25 does not directly reward:
    * **name coverage** — fraction of the candidate's name tokens present in the
      query (rewards precise, on-topic tool names);
    * **query overlap** — fraction of query tokens present in name+description.
    An exact name-in-query hit gets a fixed boost. Range ~[0, 2].
    """
    q = set(_tokens(query))
    if not q:
        return 0.0
    name, desc = _candidate_text(candidate)
    name_tokens = set(_tokens(name))
    doc_tokens = name_tokens | set(_tokens(desc))
    name_coverage = (len(name_tokens & q) / len(name_tokens)) if name_tokens else 0.0
    query_overlap = len(q & doc_tokens) / len(q)
    boost = 0.5 if name and name.lower() in query.lower() else 0.0
    return name_coverage + query_overlap + boost


def listwise_rerank(
    query: str,
    candidates: Sequence[Any],
    *,
    scorer: Callable[[str, Any], float] | None = None,
    top_k: int | None = None,
) -> list[Any]:
    """Return ``candidates`` reordered best-first by a listwise relevance score.

    ``scorer`` defaults to :func:`lexical_listwise_score`; inject an LLM-backed
    scorer in production. The sort is **stable**, so candidates with equal scores
    keep their incoming (retrieval) order — a strict reordering, never a drop.
    Only the first ``top_k`` candidates are considered when given; the remainder
    are appended unchanged after the reranked head.
    """
    if not candidates:
        return []
    score = scorer or lexical_listwise_score
    head = list(candidates) if top_k is None else list(candidates[:top_k])
    tail = [] if top_k is None else list(candidates[top_k:])
    indexed = list(enumerate(head))
    indexed.sort(key=lambda pair: (-score(query, pair[1]), pair[0]))
    return [c for _, c in indexed] + tail


def sad_redecompose(
    request: str,
    candidate_hints: Sequence[Any],
    llm_call: Callable[[str], str],
    *,
    max_subtasks: int = 8,
) -> list[str]:
    """Skill-Aware Decomposition: re-decompose ``request`` anchored on candidates.

    Feeds the retrieved candidate names + descriptions to ``llm_call`` as
    vocabulary hints and parses the returned newline/JSON list of atomic
    sub-tasks. ``llm_call`` is injected (a stub in tests, the agent's model in
    production). Never raises — a bad/empty model reply yields ``[]``.
    """
    hints_lines = []
    for c in candidate_hints:
        name, desc = _candidate_text(c)
        if name:
            hints_lines.append(f"- {name}: {desc}".rstrip())
    hints = "\n".join(hints_lines) if hints_lines else "(no candidates)"
    prompt = (
        "Decompose the request into atomic sub-tasks, each mapped to ONE of the "
        "candidate skills/tools below. Use the candidate names as anchors. Return "
        "one sub-task per line as `sub-task -> candidate_name`.\n\n"
        f"Request: {request}\n\nCandidates:\n{hints}\n"
    )
    try:
        reply = llm_call(prompt) or ""
    except Exception:
        return []
    subtasks: list[str] = []
    for line in reply.splitlines():
        line = line.strip().lstrip("-*0123456789. ").strip()
        if line:
            subtasks.append(line)
        if len(subtasks) >= max_subtasks:
            break
    return subtasks


def maybe_rerank_hits(
    query: str,
    hits: Sequence[Any],
    config: SkillRoutingConfig | None = None,
    *,
    scorer: Callable[[str, Any], float] | None = None,
) -> list[Any]:
    """Runtime seam: listwise-rerank ``hits`` iff listwise reranking is enabled.

    Returns ``list(hits)`` unchanged (same order) when disabled, so the default
    ``tool_search`` behavior is preserved. Never raises — any error degrades to
    the original order.
    """
    cfg = config or load_skill_routing_config()
    hits_list = list(hits)
    if not cfg.listwise_rerank_enabled or not hits_list:
        return hits_list
    try:
        return listwise_rerank(query, hits_list, scorer=scorer, top_k=cfg.rerank_top_k)
    except Exception:
        return hits_list
