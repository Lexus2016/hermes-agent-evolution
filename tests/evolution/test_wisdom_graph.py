# -*- coding: utf-8 -*-
"""Unit tests for Wisdom Graph and PCR triplet representation (#2385)."""

from pathlib import Path
import pytest

from evolution.lib.wisdom_graph import (
    PCRTriplet,
    WisdomGraph,
)


class TestWisdomGraph:
    """Test suite for WisdomGraph and PCR triplet operations."""

    def test_pcr_triplet_serialization(self):
        triplet = PCRTriplet(
            primary_insight="Node-level caching prevents redundant LLM calls",
            context_condition="Pipeline stages with identical deterministic inputs",
            resultant_action="Wrap stage dispatch with StageCache.cache_call",
            sufficiency_score=0.95,
            necessity_score=0.85,
            provenance_sources=["arXiv:2608.10504"],
        )
        d = triplet.to_dict()
        assert d["sufficiency_score"] == 0.95
        assert len(d["triplet_id"]) == 12

        restored = PCRTriplet.from_dict(d)
        assert restored.primary_insight == triplet.primary_insight
        assert restored.triplet_id == triplet.triplet_id

    def test_extract_pcr_from_text(self):
        wg = WisdomGraph()
        sample_text = """
        Insight: Contract-driven compression preserves essential rules
        Condition: Skill size exceeds prompt budget
        Directive: Run compress_skill_mdl before deploying skill
        """
        triplet = wg.extract_pcr_from_text(sample_text, provenance="research_doc.md")
        assert "Contract-driven compression" in triplet.primary_insight
        assert "Skill size exceeds" in triplet.context_condition
        assert "compress_skill_mdl" in triplet.resultant_action
        assert triplet.provenance_sources == ["research_doc.md"]

    def test_query_and_reasoning(self):
        wg = WisdomGraph()

        t1 = PCRTriplet(
            primary_insight="ExRole induces executable role prototypes from trajectories",
            context_condition="Subagent task delegation without predefined roles",
            resultant_action="Call suggest_delegation_role with task goal",
            sufficiency_score=0.9,
            necessity_score=0.9,
        )
        t2 = PCRTriplet(
            primary_insight="Verification scope enforces least agency boundary",
            context_condition="Delegated tasks with real tool access",
            resultant_action="Check file and command boundaries with VerificationScopeEnforcer",
            sufficiency_score=1.0,
            necessity_score=0.9,
        )
        wg.add_triplet(t1)
        wg.add_triplet(t2)

        # Query
        results = wg.query("subagent delegation role", top_k=5)
        assert len(results) >= 1
        assert results[0].triplet_id == t1.triplet_id

        # Deductive reasoning (given context, what actions to take?)
        actions = wg.deductive_reasoning("delegated tasks with tool access")
        assert len(actions) >= 1
        assert "VerificationScopeEnforcer" in actions[0]

        # Abductive reasoning (given observed result/action, what is the hypothesis?)
        hypotheses = wg.abductive_reasoning("suggest_delegation_role")
        assert len(hypotheses) >= 1
        assert hypotheses[0].triplet_id == t1.triplet_id

    def test_seed_epoch_attribution(self):
        # Simulate runs across 2 seeds and 2 strategies
        # Strategy A consistently adds +10 over baseline regardless of seed
        # Strategy B is lower by -10
        runs = [
            {"strategy": "StrategyA", "seed": 42, "score": 80.0},
            {"strategy": "StrategyB", "seed": 42, "score": 60.0},
            {"strategy": "StrategyA", "seed": 100, "score": 90.0},
            {"strategy": "StrategyB", "seed": 100, "score": 70.0},
        ]
        # Seed 42 mean = 70.0 -> A is +10, B is -10
        # Seed 100 mean = 80.0 -> A is +10, B is -10
        attribution = WisdomGraph.seed_epoch_attribution(runs)
        assert attribution["StrategyA"] == 10.0
        assert attribution["StrategyB"] == -10.0

    def test_save_load_json(self, tmp_path: Path):
        wg = WisdomGraph()
        wg.add_triplet(
            PCRTriplet(
                primary_insight="Causal discovery prunes zero-effect edges",
                context_condition="Hydra orchestrator dispatch loops",
                resultant_action="Prune stale timer triggers",
            )
        )
        save_file = tmp_path / "wisdom.json"
        wg.save_json(save_file)

        loaded = WisdomGraph.load_json(save_file)
        assert len(loaded.triplets) == 1
        triplet = list(loaded.triplets.values())[0]
        assert "Causal discovery" in triplet.primary_insight
