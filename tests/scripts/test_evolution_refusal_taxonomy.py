"""Tests for evolution_refusal_taxonomy.py — refusal taxonomy and recovery (#756)."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
from evolution_refusal_taxonomy import (  # noqa: E402  # fmt: skip
    detect_refusals,
    classify_refusal,
    recovery_suggestion,
    analyze_session,
    main,
    OVER_REFUSAL,
    TRUE_CAPABILITY_GAP,
    MISSING_SKILL_TRIGGER,
    PERMISSION_BOUNDARY,
    RECOVERABLE_ERROR,
    SPURIOUS,
)


def test_detect():
    r = detect_refusals("I can't do that for you.")
    assert len(r) == 1 and r[0]["is_refusal"]
    assert not detect_refusals("I can't stress enough.")[0][
        "is_refusal"
    ]  # false positive
    assert len(detect_refusals("I can't help. I don't have access.")) == 2
    assert detect_refusals("Sure, here is the result.") == []


def test_classify():
    assert classify_refusal("I can't do that.") == OVER_REFUSAL
    assert classify_refusal("I don't have a tool for that.") == TRUE_CAPABILITY_GAP
    assert (
        classify_refusal("I don't have permission to access that security zone.")
        == PERMISSION_BOUNDARY
    )
    assert (
        classify_refusal("I can't do that.", has_tool_failure=True) == RECOVERABLE_ERROR
    )
    assert (
        classify_refusal("I can't do that.", skill_available=True)
        == MISSING_SKILL_TRIGGER
    )
    assert classify_refusal("Sure, here you go.") == SPURIOUS


def test_recovery():
    for cat in (OVER_REFUSAL, TRUE_CAPABILITY_GAP, PERMISSION_BOUNDARY):
        assert len(recovery_suggestion(cat)) > 10


def test_analyze():
    r = analyze_session("Here is the result you asked for.")
    assert not r["has_refusals"] and r["refusal_count"] == 0
    r = analyze_session("I can't do that. I don't have access.")
    assert r["has_refusals"] and r["refusal_count"] >= 1
    assert len(r["categories"]) > 0 and len(r["suggestions"]) > 0


def test_cli(capsys, monkeypatch):
    import io

    assert main(["x", "detect", "I can't help you."]) == 0
    assert json.loads(capsys.readouterr().out)[0]["is_refusal"]
    assert main(["x", "classify", "I can't do that."]) == 0
    assert json.loads(capsys.readouterr().out)["category"] == OVER_REFUSAL
    monkeypatch.setattr(sys, "stdin", io.StringIO("I can't help. No access."))
    assert main(["x", "analyze"]) == 0
    assert json.loads(capsys.readouterr().out)["has_refusals"]
    assert main(["x"]) == 2 and main(["x", "frob"]) == 2
