# -*- coding: utf-8 -*-
"""Unit tests for references-not-rules memory framing + consensus (#2576)."""

from evolution.lib.memory_framing import (
    ConsensusVerdict,
    REFERENCES_NOT_RULES_PROMPT,
    consensus_validate,
    frame_memory_block,
)


class TestReferencesNotRulesPrompt:
    def test_prompt_mentions_references_not_rules(self):
        assert "REFERENCES" in REFERENCES_NOT_RULES_PROMPT
        assert "NOT RULES" in REFERENCES_NOT_RULES_PROMPT
        assert "corroborate" in REFERENCES_NOT_RULES_PROMPT

    def test_frame_memory_block_includes_framing(self):
        block = frame_memory_block("MEMORY (your personal notes)", ["entry one"])
        assert REFERENCES_NOT_RULES_PROMPT in block
        assert "MEMORY (your personal notes)" in block
        assert "entry one" in block

    def test_frame_memory_block_without_framing(self):
        block = frame_memory_block(
            "MEMORY (your personal notes)", ["entry one"], include_framing=False
        )
        assert REFERENCES_NOT_RULES_PROMPT not in block
        assert "entry one" in block


class TestConsensusValidate:
    def test_high_confidence_when_corroborated(self):
        verdict = consensus_validate(
            "The user prefers Python for data analysis",
            [
                {
                    "source": "user_message",
                    "content": "I always use Python for data analysis",
                }
            ],
        )
        assert verdict.confidence == "high"
        assert "user_message" in verdict.corroborated_by
        assert verdict.contradicted_by == []

    def test_low_confidence_when_contradicted(self):
        verdict = consensus_validate(
            "The user prefers Python for data analysis",
            [
                {
                    "source": "user_message",
                    "content": "I do not use Python for data analysis",
                }
            ],
        )
        assert verdict.confidence == "low"
        assert "user_message" in verdict.contradicted_by

    def test_medium_confidence_when_neutral(self):
        verdict = consensus_validate(
            "The user prefers Python for data analysis",
            [{"source": "tool_output", "content": "ls: no such file"}],
        )
        assert verdict.confidence == "medium"
        assert verdict.corroborated_by == []
        assert verdict.contradicted_by == []

    def test_empty_evidence_is_medium(self):
        verdict = consensus_validate("some memory entry", [])
        assert verdict.confidence == "medium"

    def test_verdict_serialization(self):
        v = ConsensusVerdict(
            entry_text="e",
            confidence="high",
            corroborated_by=["a"],
            contradicted_by=[],
            notes="n",
        )
        d = v.to_dict()
        restored = ConsensusVerdict.from_dict(d)
        assert restored.entry_text == "e"
        assert restored.confidence == "high"
        assert restored.corroborated_by == ["a"]
