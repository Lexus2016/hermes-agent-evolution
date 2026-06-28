"""Tests for scripts/evolution_realized_impact.py — post-merge realized-impact loop."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from evolution_realized_impact import (  # noqa: E402
    compute_realized,
    format_realized,
    load_ledger,
)


def _merge(issue, merged_at, predicted=0.8, target="fix X"):
    return {"issue": issue, "merged_at": merged_at, "predicted_impact": predicted, "target": target}


def _verdict(issue, verdict, verified_at="2026-06-20", note=""):
    return {"issue": issue, "verdict": verdict, "verified_at": verified_at, "note": note}


class TestLoadLedger:
    def test_missing_file_returns_empty(self, tmp_path):
        assert load_ledger(tmp_path / "nope.jsonl") == []

    def test_folds_merge_and_verdict_lines_for_same_issue(self, tmp_path):
        f = tmp_path / "ledger.jsonl"
        f.write_text(
            "\n".join(
                [
                    '{"issue": 1, "merged_at": "2026-06-01", "predicted_impact": 0.9, "target": "t1"}',
                    '{"issue": 2, "merged_at": "2026-06-02", "predicted_impact": 0.5, "target": "t2"}',
                    '{"issue": 1, "verdict": "confirmed", "verified_at": "2026-06-10"}',
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        recs = load_ledger(f)
        assert len(recs) == 2  # folded by issue, original order preserved
        assert recs[0]["issue"] == 1
        assert recs[0]["verdict"] == "confirmed"  # verdict line merged in
        assert recs[0]["target"] == "t1"  # merge metadata retained
        assert "verdict" not in recs[1]

    def test_malformed_lines_skipped(self, tmp_path):
        f = tmp_path / "ledger.jsonl"
        f.write_text(
            'not json\n{"no_issue": true}\n{"issue": 5, "merged_at": "2026-06-01"}\n',
            encoding="utf-8",
        )
        recs = load_ledger(f)
        assert len(recs) == 1 and recs[0]["issue"] == 5


class TestComputeRealized:
    def test_rate_and_confirmed_count(self):
        recs = [
            _merge(1, "2026-06-01") | _verdict(1, "confirmed"),
            _merge(2, "2026-06-02") | _verdict(2, "confirmed"),
            _merge(3, "2026-06-03") | _verdict(3, "no-signal"),
        ]
        h = compute_realized(recs, today="2026-06-30")
        assert h["verified"] == 3
        assert h["confirmed"] == 2
        assert h["realized_impact_rate"] == round(2 / 3, 3)

    def test_consecutive_miss_streak_flags_low_impact(self):
        recs = [
            _merge(1, "2026-06-01") | _verdict(1, "confirmed"),
            _merge(2, "2026-06-02") | _verdict(2, "regressed"),
            _merge(3, "2026-06-03") | _verdict(3, "no-signal"),
            _merge(4, "2026-06-04") | _verdict(4, "regressed"),
        ]
        h = compute_realized(recs, today="2026-06-30", streak_k=3)
        assert h["miss_streak"] == 3
        assert any("REALIZED_IMPACT_LOW" in f for f in h["flags"])

    def test_matured_unverified_backlog_flagged(self):
        # merged long ago, never verified -> the verification step isn't running
        recs = [_merge(i, "2026-06-01") for i in range(1, 5)]
        h = compute_realized(recs, today="2026-06-30", maturity_days=5)
        assert h["verified"] == 0
        assert h["matured_unverified"] == 4
        assert any("UNVERIFIED_BACKLOG" in f for f in h["flags"])

    def test_recent_unverified_not_counted_as_matured(self):
        recs = [_merge(1, "2026-06-29")]  # merged yesterday
        h = compute_realized(recs, today="2026-06-30", maturity_days=5)
        assert h["matured_unverified"] == 0
        assert h["flags"] == []

    def test_healthy_when_mostly_confirmed(self):
        recs = [_merge(i, "2026-06-0%d" % i) | _verdict(i, "confirmed") for i in range(1, 4)]
        h = compute_realized(recs, today="2026-06-30")
        assert h["realized_impact_rate"] == 1.0
        assert h["flags"] == []


class TestFormat:
    def test_format_includes_rate_and_tail(self):
        recs = [_merge(1, "2026-06-01") | _verdict(1, "confirmed")]
        line = format_realized(compute_realized(recs, today="2026-06-30"))
        assert line.startswith("[evolution-realized-impact]")
        assert "realized_rate=" in line
        assert "healthy" in line
