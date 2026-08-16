# -*- coding: utf-8 -*-
"""Tests for the hard structural summary-volume constraint (Issue #2470)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from evolution.lib.parallel_compaction import (
    ParallelCompactionConfig,
    ParallelCompactor,
    SummaryVolumeConstraint,
    estimate_tokens,
    split_sections,
    validate_summary_volume,
)
from agent.context_compressor import ContextCompressor


def _summary(sections):
    return "\n\n".join(f"## Section {i + 1}\n{body}" for i, body in enumerate(sections))


def test_estimate_tokens_and_split_sections():
    assert estimate_tokens("") == 0
    assert estimate_tokens("fives") == 2
    sections = split_sections("## One\nbody one\n\n## Two\nbody two")
    assert [s.splitlines()[0] for s in sections] == ["## One", "## Two"]


def test_validate_compliant_and_violations():
    c = SummaryVolumeConstraint(section_count=3, per_section_token_cap=200)
    assert validate_summary_volume(_summary(["a", "b", "c"]), c).ok is True

    # Wrong section count.
    result = validate_summary_volume(_summary(["a", "b"]), c)
    assert result.ok is False
    assert any("expected 3 sections" in v for v in result.violations)

    # Overlong section.
    tight = SummaryVolumeConstraint(section_count=2, per_section_token_cap=5)
    over = validate_summary_volume(_summary(["x" * 200, "ok"]), tight)
    assert over.ok is False
    assert over.overlong_sections == [1]

    # Disabled constraint always validates.
    assert validate_summary_volume(
        "anything", SummaryVolumeConstraint(enabled=False)
    ).ok


def test_config_nested_roundtrip():
    config = ParallelCompactionConfig(
        summary_constraint=SummaryVolumeConstraint(
            section_count=4, per_section_token_cap=60
        )
    )
    restored = ParallelCompactionConfig.from_dict(config.to_dict())
    assert restored.summary_constraint is not None
    assert restored.summary_constraint.section_count == 4
    assert restored.summary_constraint.per_section_token_cap == 60


def test_instruction_and_reprompt():
    compactor = ParallelCompactor(
        config=ParallelCompactionConfig(
            summary_constraint=SummaryVolumeConstraint(
                section_count=2, per_section_token_cap=5
            )
        )
    )
    instruction = compactor.build_summary_instruction()
    assert "EXACTLY 2" in instruction
    assert "5 tokens" in instruction
    assert ParallelCompactor().build_summary_instruction() == ""

    bad = _summary(["x" * 200, "ok"])
    result = compactor.validate_summary(bad)
    assert result.ok is False
    assert "section 1 exceeds" in compactor.build_reprompt_instruction(result)

    good = _summary(["a", "b"])
    assert compactor.validate_summary(good).ok is True
    assert compactor.build_reprompt_instruction(compactor.validate_summary(good)) == ""


def test_context_compressor_uses_summary_volume_constraint():
    """Verify ContextCompressor wires SummaryVolumeConstraint into prompt and validation."""
    constraint = SummaryVolumeConstraint(section_count=6, per_section_token_cap=250)
    config = ParallelCompactionConfig(summary_constraint=constraint)
    compressor = ContextCompressor(
        model="mock-model",
        api_key="mock-key",
        base_url="http://mock",
    )
    compressor._parallel_compactor = ParallelCompactor(config)

    # Verify prompt includes structural constraint instruction
    with patch("agent.context_compressor.call_llm") as mock_llm:
        mock_resp = MagicMock()
        mock_resp.choices = [
            MagicMock(
                message=MagicMock(
                    content="## Section 1\nContent 1\n\n## Section 2\nContent 2"
                )
            )
        ]
        mock_llm.return_value = mock_resp

        sample_turns = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi there"},
        ]
        result = compressor._generate_summary(sample_turns)
        assert result is not None

        # Verify call_llm was passed the prompt containing the structural constraint
        called_prompt = mock_llm.call_args[1]["messages"][0]["content"]
        assert "STRUCTURAL CONSTRAINT:" in called_prompt
        assert "EXACTLY 6" in called_prompt
