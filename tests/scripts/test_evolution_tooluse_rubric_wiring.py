"""Wiring tests for evolution_tooluse_rubric.py (#1268).

Verifies the five-dimension tool-use competency rubric: all five
dimensions are computed, the repeated-identical-call detector flags a
spiral pattern, discovery scoring penalises redundant search, and the
rubric report aggregates scores + clusters. Covers invariants.
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

import evolution_tooluse_rubric as tr  # noqa: E402


def _sample_payload() -> dict:
    return {
        "calls": [
            # A spiral: patch with the SAME args 4 times in a row (threshold 3).
            {
                "tool": "patch",
                "args": {"path": "f.py", "old_string": "a", "new_string": "b"},
                "succeeded": False,
                "error": "no match",
                "turn": 0,
            },
            {
                "tool": "patch",
                "args": {"path": "f.py", "old_string": "a", "new_string": "b"},
                "succeeded": False,
                "error": "no match",
                "turn": 1,
            },
            {
                "tool": "patch",
                "args": {"path": "f.py", "old_string": "a", "new_string": "b"},
                "succeeded": False,
                "error": "no match",
                "turn": 2,
            },
            {
                "tool": "patch",
                "args": {"path": "f.py", "old_string": "a", "new_string": "b"},
                "succeeded": False,
                "error": "no match",
                "turn": 3,
            },
            # A discovery call (redundant search).
            {
                "tool": "tool_search",
                "args": {"query": "patch"},
                "succeeded": True,
                "turn": 4,
            },
            # A successful, unique call.
            {
                "tool": "terminal",
                "args": {"command": "ls"},
                "succeeded": True,
                "turn": 5,
            },
            # A syntax error.
            {
                "tool": "tool_call",
                "args": {},
                "succeeded": False,
                "error": "json parse error: invalid argument",
                "turn": 6,
            },
        ],
        "repeat_threshold": 3,
    }


def test_all_five_dimensions_computed():
    report = tr.evaluate(_sample_payload())
    scores = report["scores"]
    for dim in (
        "discovery",
        "parameterization",
        "syntax",
        "error_recovery",
        "efficiency",
    ):
        assert dim in scores
        assert 0.0 <= scores[dim] <= 1.0
    assert "overall" in scores


def test_repeated_call_cluster_detected():
    report = tr.evaluate(_sample_payload())
    clusters = report["repeated_call_clusters"]
    assert len(clusters) >= 1
    spiral = clusters[0]
    assert spiral["tool"] == "patch"
    assert spiral["count"] == 4
    assert spiral["start_turn"] == 0
    assert spiral["end_turn"] == 3


def test_discovery_penalises_redundant_search():
    report = tr.evaluate(_sample_payload())
    # 1 discovery call out of 7 total -> ratio 1/7, score = 1 - 1/7 ≈ 0.857.
    assert report["scores"]["discovery"] < 1.0


def test_syntax_penalises_parse_error():
    report = tr.evaluate(_sample_payload())
    # One syntax error out of 7 calls -> score 6/7 ≈ 0.857.
    assert report["scores"]["syntax"] < 1.0


def test_error_recovery_penalised_by_spiral():
    report = tr.evaluate(_sample_payload())
    # The 4-call spiral wastes calls -> error_recovery < 1.0.
    assert report["scores"]["error_recovery"] < 1.0


def test_total_and_unique_calls_reported():
    report = tr.evaluate(_sample_payload())
    assert report["total_calls"] == 7
    # 4 identical patch calls have the same signature -> unique < total.
    assert report["unique_calls"] < report["total_calls"]


def test_main_returns_zero(tmp_path, capsys):
    payload_file = tmp_path / "payload.json"
    payload_file.write_text(json.dumps(_sample_payload()), encoding="utf-8")
    rc = tr.main(["--payload", str(payload_file)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "scores" in out
