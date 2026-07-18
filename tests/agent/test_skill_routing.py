# -*- coding: utf-8 -*-
"""Unit tests for compositional skill routing (#1137)."""

from __future__ import annotations

from agent.skill_routing import (
    SkillRoutingConfig,
    lexical_listwise_score,
    listwise_rerank,
    maybe_rerank_hits,
    sad_redecompose,
)


class _Cand:
    def __init__(self, name, description=""):
        self.name = name
        self.description = description

    def __repr__(self):
        return f"_Cand({self.name})"


# --- config -------------------------------------------------------------------


def test_config_defaults_off():
    cfg = SkillRoutingConfig()
    assert cfg.sad_enabled is False
    assert cfg.listwise_rerank_enabled is False


def test_config_from_mapping_accepts_both_key_spellings():
    cfg = SkillRoutingConfig.from_mapping({"sad": True, "listwise_rerank": True, "rerank_top_k": 3})
    assert cfg.sad_enabled is True
    assert cfg.listwise_rerank_enabled is True
    assert cfg.rerank_top_k == 3
    cfg2 = SkillRoutingConfig.from_mapping({"sad_enabled": True})
    assert cfg2.sad_enabled is True


def test_config_from_mapping_none():
    assert SkillRoutingConfig.from_mapping(None).listwise_rerank_enabled is False


# --- scorer -------------------------------------------------------------------


def test_score_rewards_name_match():
    q = "read a file from disk"
    high = lexical_listwise_score(q, _Cand("read_file", "read a file"))
    low = lexical_listwise_score(q, _Cand("send_email", "send an email message"))
    assert high > low


def test_score_zero_for_empty_query():
    assert lexical_listwise_score("", _Cand("read_file")) == 0.0


# --- listwise rerank ----------------------------------------------------------


def test_rerank_promotes_best_match():
    cands = [
        _Cand("send_email", "send an email"),
        _Cand("read_file", "read a file from disk"),
        _Cand("list_dir", "list a directory"),
    ]
    ranked = listwise_rerank("read a file", cands)
    assert ranked[0].name == "read_file"
    # strict reordering: same set of candidates
    assert {c.name for c in ranked} == {c.name for c in cands}


def test_rerank_is_stable_for_equal_scores():
    # two candidates with no query overlap -> equal (zero) score -> keep order
    cands = [_Cand("alpha", "xxx"), _Cand("beta", "yyy")]
    ranked = listwise_rerank("zzz", cands)
    assert [c.name for c in ranked] == ["alpha", "beta"]


def test_rerank_top_k_leaves_tail_untouched():
    cands = [_Cand(f"t{i}", "") for i in range(5)]
    # put a strong match at the tail; with top_k=2 it must NOT be promoted
    cands.append(_Cand("read_file", "read a file"))
    ranked = listwise_rerank("read a file", cands, top_k=2)
    assert ranked[-1].name == "read_file"


def test_rerank_empty():
    assert listwise_rerank("q", []) == []


def test_rerank_accepts_mapping_candidates():
    cands = [{"name": "send_email", "description": "email"}, {"name": "read_file", "description": "read a file"}]
    ranked = listwise_rerank("read a file", cands)
    assert ranked[0]["name"] == "read_file"


# --- maybe_rerank_hits seam ---------------------------------------------------


def test_seam_disabled_preserves_order():
    cands = [_Cand("send_email", "email"), _Cand("read_file", "read a file")]
    cfg = SkillRoutingConfig(listwise_rerank_enabled=False)
    out = maybe_rerank_hits("read a file", cands, cfg)
    assert [c.name for c in out] == ["send_email", "read_file"]


def test_seam_enabled_reranks():
    cands = [_Cand("send_email", "email"), _Cand("read_file", "read a file")]
    cfg = SkillRoutingConfig(listwise_rerank_enabled=True)
    out = maybe_rerank_hits("read a file", cands, cfg)
    assert out[0].name == "read_file"


def test_seam_never_raises_on_bad_scorer():
    cfg = SkillRoutingConfig(listwise_rerank_enabled=True)
    def boom(q, c):
        raise RuntimeError("nope")
    out = maybe_rerank_hits("q", [_Cand("a"), _Cand("b")], cfg, scorer=boom)
    assert [c.name for c in out] == ["a", "b"]  # degrades to original order


# --- SAD ----------------------------------------------------------------------


def test_sad_redecompose_uses_candidate_hints_and_parses_lines():
    seen_prompts = []

    def fake_llm(prompt):
        seen_prompts.append(prompt)
        return "read the config -> read_file\nsummarize it -> summarize\n"

    subtasks = sad_redecompose("do the thing", [_Cand("read_file", "read a file")], fake_llm)
    assert subtasks == ["read the config -> read_file", "summarize it -> summarize"]
    # the candidate name was injected into the prompt as an anchor
    assert "read_file" in seen_prompts[0]


def test_sad_redecompose_respects_max_subtasks():
    def fake_llm(prompt):
        return "\n".join(f"task {i} -> t" for i in range(20))
    subtasks = sad_redecompose("x", [], fake_llm, max_subtasks=3)
    assert len(subtasks) == 3


def test_sad_redecompose_handles_llm_error():
    def boom(prompt):
        raise RuntimeError("model down")
    assert sad_redecompose("x", [], boom) == []
