"""Tests for the evolution event log (issue #1940)."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "scripts"))
from evolution_event_log import (  # noqa: E402
    classify_evolution_type,
    event_summary,
    load_events,
    log_event,
    select_strategy,
    STRATEGY_PRESETS,
)


def test_strategy_classification_presets():
    assert select_strategy(25) == "repair-only" and select_strategy(0) == "balanced"
    assert classify_evolution_type(["bug"]) == "repair"
    assert classify_evolution_type(["improvement"]) == "optimization"
    assert classify_evolution_type(["enhancement"]) == "innovation"
    for name, r in STRATEGY_PRESETS.items():
        assert sum(r.values()) == 100


def test_log_load_validate(tmp_path):
    ev = log_event(
        {"action": "proposal", "evolution_type": "innovation", "issue_number": 123},
        tmp_path,
    )
    assert ev["issue_number"] == 123 and "timestamp" in ev
    log_event({"action": "merge", "evolution_type": "repair"}, tmp_path)
    events = load_events(tmp_path)
    assert len(events) == 2 and events[0]["action"] == "proposal"
    assert load_events(tmp_path / "empty") == []
    with pytest.raises(ValueError, match="invalid action"):
        log_event({"action": "bogus"}, tmp_path)
    with pytest.raises(ValueError, match="invalid evolution_type"):
        log_event({"action": "proposal", "evolution_type": "bogus"}, tmp_path)
    s = event_summary(events)
    assert (
        s["total"] == 2
        and s["by_action"]["proposal"] == 1
        and s["by_type"]["repair"] == 1
    )
