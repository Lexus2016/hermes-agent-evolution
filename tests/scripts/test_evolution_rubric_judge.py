"""Tests for deterministic claim extraction (#2482 Slice A / #2513).

Slice A requires that the rubric-judge output includes a structured list of
extracted claims (not only an aggregate score) and that extraction is
deterministic / reproducible for identical input.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from evolution_rubric_judge import _markdown_sections, extract_claims  # noqa: E402

SAMPLE = """\
# Finding 1
Adopting the parallel compactor improved merge latency by 52% across the
benchmark suite. Source: https://example.com/bench.

# Finding 2
The tokenizer change reduced prompt tokens by 64%, cutting cost per run.
Some neutral background prose with no measurable outcome here.
"""


def test_markdown_sections_keeps_order_and_preamble() -> None:
    assert [h for h, _ in _markdown_sections(SAMPLE)] == ["Finding 1", "Finding 2"]
    blocks = _markdown_sections("lead-in prose\n\n# Head\nbody text")
    assert blocks[0][0] == "" and blocks[1][0] == "Head"


def test_extract_claims_emits_structured_list() -> None:
    claims = extract_claims(("research", SAMPLE))
    assert claims and {"claim", "source", "evidence_url"} <= set(claims[0])
    assert claims[0]["source"]["stage"] == "research"


def test_extract_claims_picks_outcome_sentences() -> None:
    texts = [c["claim"] for c in extract_claims(("research", SAMPLE))]
    assert any("improved merge latency" in t for t in texts)
    assert any("reduced prompt tokens" in t for t in texts)
    assert not any("neutral background prose" in t for t in texts)


def test_extract_claims_deterministic_for_identical_input() -> None:
    a = extract_claims(("r", SAMPLE), ("i", "# X\nFixed issue #7."))
    b = extract_claims(("r", SAMPLE), ("i", "# X\nFixed issue #7."))
    assert a == b


def test_extract_claims_dedupes_and_sorts() -> None:
    text = (
        "## A\nSwitching the index improved lookup speed by 30% overall.\n\n"
        "## B\nSwitching the index improved lookup speed by 30% overall."
    )
    claims = extract_claims(("research", text))
    assert [c["source"]["section"] for c in claims] == ["A", "B"]


def test_extract_claims_ignores_none_and_empty() -> None:
    assert extract_claims(("research", None), ("implementation", "")) == []
