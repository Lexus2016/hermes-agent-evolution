"""Tests for evolution_floor_gate.py (#1809, parent #1267)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from evolution_floor_gate import (  # noqa: E402
    DEFAULT_FLOOR_SCORES,
    FloorTestResult,
    check_floor_gate,
    load_floor_scores,
    main,
)


def test_below_floor_blocks():
    """A PR score at or below the floor is blocked."""
    r = check_floor_gate({"mean_total": 0.0}, {"mean_total": 0.0})
    assert r.blocked and len(r.violations) == 1


def test_above_floor_passes():
    """A PR score well above the floor passes."""
    r = check_floor_gate({"mean_total": 0.5}, {"mean_total": 0.0})
    assert not r.blocked


def test_at_margin_threshold():
    """Score at the margin threshold (floor * 1.05) is blocked."""
    r = check_floor_gate({"mean_total": 0.05}, {"mean_total": 0.05}, floor_margin=0.0)
    assert r.blocked


def test_missing_metric_skipped():
    """Metrics not in floor_scores are silently skipped."""
    r = check_floor_gate({"unknown": 0.0}, {"mean_total": 0.0})
    assert not r.blocked


def test_default_floors_used():
    """When no floor_scores provided, defaults are used."""
    r = check_floor_gate({"mean_total": 0.0})
    assert r.blocked
    assert r.floor_scores == DEFAULT_FLOOR_SCORES


def test_load_floor_scores_from_jsonl(tmp_path):
    """Floor scores can be loaded from a JSONL file."""
    f = tmp_path / "scores.jsonl"
    f.write_text(json.dumps({"total": 0.1}) + "\n" + json.dumps({"total": 0.2}) + "\n")
    scores = load_floor_scores(str(f))
    assert "mean_total" in scores
    assert abs(scores["mean_total"] - 0.15) < 0.001


def test_load_floor_scores_nonexistent():
    """Non-existent file returns defaults."""
    scores = load_floor_scores("/nonexistent/path.jsonl")
    assert scores == DEFAULT_FLOOR_SCORES


def test_load_floor_scores_none():
    """None path returns defaults."""
    assert load_floor_scores(None) == DEFAULT_FLOOR_SCORES


def test_main_blocks(capsys):
    rc = main(["--pr-scores", '{"mean_total": 0.0}'])
    assert rc == 1
    out = capsys.readouterr().out
    assert "blocked" in out


def test_main_passes(capsys):
    rc = main(["--pr-scores", '{"mean_total": 0.8}'])
    assert rc == 0
