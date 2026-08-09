"""Tests for scripts/evolution_genomic_events.py."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import evolution_genomic_events as evo  # noqa: E402


def test_select_strategies():
    assert evo.select_strategy(10, 100) == "balanced"
    assert evo.select_strategy(55, 100) == "harden"
    assert evo.select_strategy(85, 100) == "repair-only"


def test_get_preset_valid():
    p = evo.get_preset("balanced")
    assert p["innovation"] == 50 and sum(p.values()) == 100


def test_get_preset_invalid():
    try:
        evo.get_preset("unknown")
        assert False
    except KeyError:
        pass


def test_classify_types():
    assert evo.classify_evolution_type("[FIX] tool crashes") == "repair"
    assert (
        evo.classify_evolution_type("[IMPROVEMENT] optimize pipeline") == "optimization"
    )
    assert evo.classify_evolution_type("[FEATURE] new agent capability") == "innovation"


def test_log_event(tmp_path):
    log = tmp_path / "events.jsonl"
    e = evo.log_event("proposal", "innovation", "test desc", 42, log)
    assert e["event_type"] == "proposal"
    assert e["issue_number"] == 42
    lines = log.read_text().strip().split("\n")
    assert len(lines) == 1
    assert json.loads(lines[0])["description"] == "test desc"


def test_log_event_invalid_type(tmp_path):
    try:
        evo.log_event("bad", "innovation", "x", event_log=tmp_path / "e.jsonl")
        assert False
    except ValueError:
        pass


def test_load_events(tmp_path):
    log = tmp_path / "events.jsonl"
    evo.log_event("proposal", "repair", "a", 1, log)
    evo.log_event("implementation", "optimization", "b", 2, log)
    events = evo.load_events(log)
    assert len(events) == 2
    assert events[0]["evolution_type"] == "repair"


def test_load_events_empty(tmp_path):
    assert evo.load_events(tmp_path / "nonexistent.jsonl") == []


def test_summary():
    events = [
        {"event_type": "proposal", "evolution_type": "innovation"},
        {"event_type": "proposal", "evolution_type": "repair"},
        {"event_type": "revert", "evolution_type": "repair"},
    ]
    s = evo.summary(events)
    assert s["total"] == 3
    assert s["by_event_type"]["proposal"] == 2
    assert s["by_evolution_type"]["repair"] == 2
