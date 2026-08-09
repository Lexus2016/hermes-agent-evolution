"""Tests for the evolution event log (issue #1940)."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "scripts"))
from evolution_event_log import (  # noqa: E402
    classify_evolution_type,
    event_summary,
    load_events,
    log_event,
    select_strategy,
    STRATEGY_PRESETS,
)


def test_select_strategy_backlog_full():
    assert select_strategy(25) == "repair-only"
    assert select_strategy(30) == "repair-only"


def test_select_strategy_backlog_clear():
    assert select_strategy(0) == "balanced"
    assert select_strategy(10) == "balanced"


def test_classify_evolution_type():
    assert classify_evolution_type(["bug"]) == "repair"
    assert classify_evolution_type(["bug", "research-generated"]) == "repair"
    assert classify_evolution_type(["improvement"]) == "optimization"
    assert classify_evolution_type(["enhancement"]) == "innovation"
    assert classify_evolution_type([]) == "innovation"


def test_log_event_appends(tmp_path):
    ev = log_event(
        {
            "action": "proposal",
            "evolution_type": "innovation",
            "issue_number": 123,
            "description": "test",
        },
        tmp_path,
    )
    assert ev["action"] == "proposal"
    assert ev["issue_number"] == 123
    log_path = tmp_path / "evolution_events.jsonl"
    assert log_path.exists()
    lines = log_path.read_text().strip().split("\n")
    assert len(lines) == 1
    data = json.loads(lines[0])
    assert data["action"] == "proposal"
    assert "timestamp" in data


def test_log_event_multiple(tmp_path):
    log_event({"action": "proposal", "evolution_type": "innovation"}, tmp_path)
    log_event({"action": "merge", "evolution_type": "repair"}, tmp_path)
    events = load_events(tmp_path)
    assert len(events) == 2
    assert events[0]["action"] == "proposal"
    assert events[1]["action"] == "merge"


def test_log_event_validates_action(tmp_path):
    import pytest

    with pytest.raises(ValueError, match="invalid action"):
        log_event({"action": "bogus", "evolution_type": "innovation"}, tmp_path)


def test_log_event_validates_type(tmp_path):
    import pytest

    with pytest.raises(ValueError, match="invalid evolution_type"):
        log_event({"action": "proposal", "evolution_type": "bogus"}, tmp_path)


def test_load_events_empty(tmp_path):
    assert load_events(tmp_path) == []


def test_event_summary():
    events = [
        {"action": "proposal", "evolution_type": "innovation"},
        {"action": "proposal", "evolution_type": "repair"},
        {"action": "merge", "evolution_type": "repair"},
    ]
    s = event_summary(events)
    assert s["total"] == 3
    assert s["by_action"]["proposal"] == 2
    assert s["by_action"]["merge"] == 1
    assert s["by_type"]["innovation"] == 1
    assert s["by_type"]["repair"] == 2


def test_strategy_presets_sum():
    for name, ratios in STRATEGY_PRESETS.items():
        assert sum(ratios.values()) == 100, f"{name} ratios don't sum to 100"
