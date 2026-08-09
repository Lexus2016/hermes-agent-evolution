"""Tests for the volatility-tagged memory gate (issue #1938)."""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "scripts"))
from evolution_volatility_gate import (  # noqa: E402
    ab_release_test,
    anti_recitation_check,
    classify_content_volatility,
    list_notes,
    load_volatility_index,
    tag_note,
    volatility_summary,
)


def test_tag_note_valid(tmp_path):
    index = tag_note("note-1", "volatile", tmp_path)
    assert index["note-1"] == "volatile"
    assert (tmp_path / "volatility_index.json").exists()


def test_tag_note_invalid_level(tmp_path):
    with pytest.raises(ValueError, match="invalid volatility"):
        tag_note("note-1", "bogus", tmp_path)


def test_load_index_empty(tmp_path):
    assert load_volatility_index(tmp_path) == {}


def test_load_index_after_tagging(tmp_path):
    tag_note("note-1", "volatile", tmp_path)
    tag_note("note-2", "stable", tmp_path)
    index = load_volatility_index(tmp_path)
    assert index["note-1"] == "volatile"
    assert index["note-2"] == "stable"


def test_classify_volatile_url():
    assert classify_content_volatility("https://api.example.com/v2") == "volatile"


def test_classify_volatile_version():
    assert classify_content_volatility("using v2.1.0 of the SDK") == "volatile"


def test_classify_stable():
    assert classify_content_volatility("use a binary search algorithm") == "stable"


def test_anti_recitation_detects_volatile_note():
    vi = {"note-api": "volatile", "note-algo": "stable"}
    warnings = anti_recitation_check("hardcoding note-api here", vi)
    assert len(warnings) >= 1
    assert any(w["type"] == "volatile_recitation" for w in warnings)


def test_anti_recitation_detects_url_pattern():
    warnings = anti_recitation_check("endpoint is https://api.x.com", {})
    assert any(w["type"] == "volatile_pattern" for w in warnings)


def test_anti_recitation_clean_content():
    assert anti_recitation_check("use binary search", {}) == []


def test_ab_release_can_archive():
    result = ab_release_test("note-1", 0.85, 0.84, threshold=0.05)
    assert result["can_archive"] is True
    assert result["decision"] == "archive"


def test_ab_release_must_restore():
    result = ab_release_test("note-1", 0.85, 0.70, threshold=0.05)
    assert result["can_archive"] is False
    assert result["decision"] == "restore"


def test_list_notes_filtered(tmp_path):
    tag_note("n1", "volatile", tmp_path)
    tag_note("n2", "stable", tmp_path)
    tag_note("n3", "volatile", tmp_path)
    volatile_only = list_notes("volatile", tmp_path)
    assert len(volatile_only) == 2
    assert "n2" not in volatile_only


def test_list_notes_invalid_filter(tmp_path):
    with pytest.raises(ValueError):
        list_notes("bogus", tmp_path)


def test_volatility_summary():
    index = {"n1": "volatile", "n2": "stable", "n3": "stable", "n4": "strategic"}
    s = volatility_summary(index)
    assert s["volatile"] == 1
    assert s["stable"] == 2
    assert s["strategic"] == 1
    assert s["total"] == 4
