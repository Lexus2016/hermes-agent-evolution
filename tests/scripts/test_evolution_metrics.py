"""Tests for scripts/evolution_metrics.py — longitudinal meta-evolution health."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from evolution_metrics import compute_health, format_health  # noqa: E402


def _rec(date, created=0, selected=0, rejected=0, merged=0):
    return {
        "date": date,
        "issues_created": created,
        "selected": selected,
        "rejected": rejected,
        "merged": merged,
    }


class TestComputeHealth:
    def test_idle_and_artifact_cycles_excluded_from_active(self):
        recs = [
            _rec("d1", selected=2, merged=1),
            _rec("d2"),  # idle / all-zero artifact — must NOT count as a failed cycle
            _rec("d3", created=3, selected=0, merged=0),  # active (created>0), no merge
        ]
        h = compute_health(recs)
        assert h["cycles_total"] == 3
        assert h["cycles_active"] == 2  # d2 excluded
        # 1 of 2 active cycles merged
        assert h["cycle_success_rate"] == 0.5

    def test_selection_efficiency_proxy(self):
        recs = [_rec(f"d{i}", selected=10, merged=3) for i in range(5)]
        h = compute_health(recs)
        assert h["selection_efficiency"] == round(15 / 50, 3)  # 0.3

    def test_low_efficiency_flagged_only_with_enough_signal(self):
        # < 3 active cycles → no judgement (insufficient signal)
        few = [_rec("d1", selected=10, merged=0), _rec("d2", selected=10, merged=0)]
        assert compute_health(few)["flags"] == []
        # >= 3 active cycles with poor efficiency → flagged
        many = [_rec(f"d{i}", selected=10, merged=0) for i in range(4)]
        flags = compute_health(many)["flags"]
        assert any("LOW_SUCCESS" in f for f in flags)
        assert any("LOW_SELECTION_EFFICIENCY" in f for f in flags)

    def test_healthy_pipeline_no_flags(self):
        recs = [_rec(f"d{i}", selected=4, merged=3, rejected=1) for i in range(5)]
        h = compute_health(recs)
        assert h["flags"] == []
        assert "healthy" in format_health(h)

    def test_empty_history_no_crash(self):
        h = compute_health([])
        assert h["cycles_active"] == 0
        assert h["cycle_success_rate"] is None
        assert h["selection_efficiency"] is None
        assert "[evolution-metrics]" in format_health(h)
        assert "n/a" in format_health(h)

    def test_merged_trend(self):
        improving = [_rec(f"d{i}", selected=1, merged=m) for i, m in enumerate([0, 0, 3, 4])]
        assert compute_health(improving)["merged_trend"] == "improving"
        declining = [_rec(f"d{i}", selected=1, merged=m) for i, m in enumerate([4, 3, 0, 0])]
        assert compute_health(declining)["merged_trend"] == "declining"

    def test_window_last_n(self):
        recs = [_rec(f"d{i}", selected=1, merged=1) for i in range(40)]
        assert compute_health(recs, last=10)["cycles_total"] == 10
