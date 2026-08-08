"""Tests for refusal telemetry analyzer (#1327)."""

import json
from pathlib import Path
from scripts.evolution_refusal_telemetry import (
    parse_refusal_telemetry_file,
    analyze_refusal_telemetry,
)


def test_analyze_refusal_telemetry(tmp_path: Path):
    log_file = tmp_path / "refusals.jsonl"
    data = [
        {"category": "no_tool", "nudge_tier": "t1", "shifted": True},
        {"category": "no_tool", "nudge_tier": "t1", "shifted": False},
        {"category": "access_denied", "nudge_tier": "t2", "shifted": False},
        {"category": "access_denied", "nudge_tier": "t2", "shifted": False},
    ]
    with open(log_file, "w") as f:
        for d in data:
            f.write(json.dumps(d) + "\n")

    records = parse_refusal_telemetry_file(log_file)
    assert len(records) == 4

    analysis = analyze_refusal_telemetry(records)
    assert analysis["total_refusals"] == 4
    assert analysis["by_category"]["no_tool"]["shift_rate"] == 0.5
    assert analysis["by_category"]["access_denied"]["shift_rate"] == 0.0
    assert "access_denied" in analysis["resisting_categories"]
