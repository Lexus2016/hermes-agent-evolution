"""Tests for the volatility-tagged memory gate (issue #1938)."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "scripts"))
from evolution_volatility_gate import (  # noqa: E402
    ab_release_test,
    anti_recitation_check,
    classify_volatility,
    list_notes,
    load_index,
    tag_note,
    volatility_summary,
)


def test_volatility(tmp_path):
    with pytest.raises(ValueError, match="invalid volatility"):
        tag_note("n1", "bogus", tmp_path)
    tag_note("n1", "volatile", tmp_path)
    tag_note("n2", "stable", tmp_path)
    tag_note("n3", "volatile", tmp_path)
    assert load_index(tmp_path)["n1"] == "volatile" and not load_index(tmp_path / "x")
    assert classify_volatility("https://api.x.com/v2") == "volatile"
    assert classify_volatility("use binary search") == "stable"
    assert any(
        w["type"] == "volatile_recitation"
        for w in anti_recitation_check("n1 here", {"n1": "volatile"})
    )
    assert not anti_recitation_check("use binary search", {})
    assert ab_release_test("n", 0.85, 0.84)["decision"] == "archive"
    assert ab_release_test("n", 0.85, 0.70)["decision"] == "restore"
    assert len(list_notes("volatile", tmp_path)) == 2
    s = volatility_summary(load_index(tmp_path))
    assert s["volatile"] == 2 and s["stable"] == 1 and s["total"] == 3
