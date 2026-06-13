"""Tests for scripts/evolution_funnel.py — deterministic per-cycle funnel."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from datetime import datetime  # noqa: E402

from evolution_funnel import append_funnel, compute_funnel, cycle_date  # noqa: E402


def _write(p: Path, obj):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj), encoding="utf-8")


class TestComputeFunnel:
    def test_full_cycle(self, tmp_path):
        d = "2026-06-13"
        _write(tmp_path / "issues" / f"{d}.json", {
            "total_proposals": 9, "proposals_passed_filter": 3,
            "issues_created": [{"number": 1}, {"number": 2}, {"number": 3}],
        })
        _write(tmp_path / "analysis" / f"{d}.json", {
            "rejected": [
                {"reason_code": "already-exists"}, {"reason_code": "already-exists"},
                {"reason_code": "out-of-scope"},
            ],
            "selected_for_implementation": [
                {"selected_reason": "score"}, {"selected_reason": "score"},
                {"selected_reason": "anti-starvation"},
            ],
        })
        _write(tmp_path / "integration" / f"{d}.json", {
            "merged": [{"pr": "1"}], "skipped": [{"pr": "2"}, {"pr": "3"}],
        })
        _write(tmp_path / "introspection" / f"{d}.json", {"patterns_found": [{"p": 1}]})

        r = compute_funnel(tmp_path, d)
        assert r["research_proposals"] == 9
        assert r["issues_created"] == 3
        assert r["selected"] == 3
        assert r["selected_by_reason"] == {"score": 2, "anti-starvation": 1}
        assert r["rejected"] == 3
        assert r["rejected_by_reason"] == {"already-exists": 2, "out-of-scope": 1}
        assert r["merged"] == 1
        assert r["skipped"] == 2
        assert r["introspection_patterns"] == 1

    def test_missing_reports_default_to_zero(self, tmp_path):
        r = compute_funnel(tmp_path, "2026-01-01")
        assert r["date"] == "2026-01-01"
        assert r["selected"] == 0 and r["merged"] == 0 and r["rejected"] == 0
        assert r["selected_by_reason"] == {} and r["rejected_by_reason"] == {}

    def test_malformed_report_does_not_crash(self, tmp_path):
        p = tmp_path / "analysis" / "2026-01-02.json"
        p.parent.mkdir(parents=True)
        p.write_text("{ not json", encoding="utf-8")
        r = compute_funnel(tmp_path, "2026-01-02")
        assert r["selected"] == 0  # treated as absent, no exception


class TestCycleDate:
    def test_morning_run_measures_yesterday(self):
        # 07:40 run -> previous cycle (yesterday)
        assert cycle_date(datetime(2026, 6, 13, 7, 40)) == "2026-06-12"

    def test_jitter_still_before_8_is_yesterday(self):
        assert cycle_date(datetime(2026, 6, 13, 7, 55)) == "2026-06-12"

    def test_after_8_is_today(self):
        assert cycle_date(datetime(2026, 6, 13, 12, 0)) == "2026-06-13"


class TestAppendFunnel:
    def test_appends_one_line(self, tmp_path):
        mf = tmp_path / "metrics.jsonl"
        append_funnel(mf, {"date": "2026-06-12", "merged": 1})
        append_funnel(mf, {"date": "2026-06-13", "merged": 2})
        lines = mf.read_text().strip().splitlines()
        assert len(lines) == 2
        assert json.loads(lines[1])["date"] == "2026-06-13"

    def test_rerun_same_date_replaces_not_duplicates(self, tmp_path):
        mf = tmp_path / "metrics.jsonl"
        append_funnel(mf, {"date": "2026-06-13", "merged": 1})
        append_funnel(mf, {"date": "2026-06-13", "merged": 5})  # re-run, corrected
        lines = mf.read_text().strip().splitlines()
        assert len(lines) == 1
        assert json.loads(lines[0])["merged"] == 5
