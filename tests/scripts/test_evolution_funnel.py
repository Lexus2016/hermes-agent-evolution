"""Tests for scripts/evolution_funnel.py — deterministic per-cycle funnel."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from datetime import datetime  # noqa: E402

from evolution_funnel import (  # noqa: E402
    append_funnel,
    compute_funnel,
    cycle_date,
    format_summary,
    load_records,
    summarize,
)


def _write(p: Path, obj):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj), encoding="utf-8")


class TestComputeFunnel:
    def test_full_cycle(self, tmp_path):
        d = "2026-06-13"
        _write(
            tmp_path / "issues" / f"{d}.json",
            {
                "total_proposals": 9,
                "proposals_passed_filter": 3,
                "issues_created": [{"number": 1}, {"number": 2}, {"number": 3}],
            },
        )
        _write(
            tmp_path / "analysis" / f"{d}.json",
            {
                "rejected": [
                    {"reason_code": "already-exists"},
                    {"reason_code": "already-exists"},
                    {"reason_code": "out-of-scope"},
                ],
                "selected_for_implementation": [
                    {"selected_reason": "score"},
                    {"selected_reason": "score"},
                    {"selected_reason": "anti-starvation"},
                ],
            },
        )
        _write(
            tmp_path / "integration" / f"{d}.json",
            {
                "merged": [{"pr": "1"}],
                "skipped": [{"pr": "2"}, {"pr": "3"}],
            },
        )
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

    def test_introspection_list_report_does_not_crash(self, tmp_path):
        # introspection emits a bare LIST of patterns, not a dict. Regression:
        # `.get()` on a list raised AttributeError and killed the whole funnel
        # job (and the realized-impact sidecar refresh riding on it).
        d = "2026-01-03"
        _write(tmp_path / "introspection" / f"{d}.json", [{"p": 1}, {"p": 2}, {"p": 3}])
        r = compute_funnel(tmp_path, d)
        assert r["introspection_patterns"] == 3  # counted straight from the list
        assert r["selected"] == 0  # other stages absent — no crash

    def test_list_shaped_reports_coerced_not_crashed(self, tmp_path):
        # Any stage report arriving as a list must degrade to 0, never crash.
        d = "2026-01-04"
        _write(tmp_path / "analysis" / f"{d}.json", ["unexpected", "list"])
        _write(tmp_path / "integration" / f"{d}.json", [{"merged": "x"}])
        r = compute_funnel(tmp_path, d)
        assert r["selected"] == 0 and r["merged"] == 0

    def test_legacy_created_key_counts_and_warns(self, tmp_path, caplog):
        # 2026-07-04 issue report used 'created' instead of 'issues_created'.
        import logging

        d = "2026-07-04"
        _write(
            tmp_path / "issues" / f"{d}.json",
            {
                "date": d,
                "created": [{"number": 734}, {"number": 735}],
            },
        )
        _write(
            tmp_path / "analysis" / f"{d}.json",
            {"selected_for_implementation": [], "rejected": []},
        )
        _write(tmp_path / "integration" / f"{d}.json", {"merged": [], "skipped": []})

        with caplog.at_level(logging.WARNING, logger="evolution_funnel"):
            r = compute_funnel(tmp_path, d)

        assert r["issues_created"] == 2
        assert "legacy key 'created'" in caplog.text
        assert "2026-07-04" in caplog.text

    def test_canonical_issues_created_key_no_warning(self, tmp_path, caplog):
        # 2026-07-05 issue report uses canonical 'issues_created' key.
        import logging

        d = "2026-07-05"
        _write(
            tmp_path / "issues" / f"{d}.json",
            {
                "issues_created": [{"number": 737}],
            },
        )
        _write(
            tmp_path / "analysis" / f"{d}.json",
            {"selected_for_implementation": [], "rejected": []},
        )
        _write(tmp_path / "integration" / f"{d}.json", {"merged": [], "skipped": []})

        with caplog.at_level(logging.WARNING, logger="evolution_funnel"):
            r = compute_funnel(tmp_path, d)

        assert r["issues_created"] == 1
        assert "legacy key" not in caplog.text

    def test_proposals_filed_legacy_key_counts_and_warns(self, tmp_path, caplog):
        # 2026-07-05 issue report briefly used 'proposals_filed' instead of
        # 'issues_created', which caused metrics.jsonl to record created=0.
        import logging

        d = "2026-07-05"
        _write(
            tmp_path / "issues" / f"{d}.json",
            {
                "date": d,
                "proposals_filed": [{"number": 734}, {"number": 735}],
            },
        )
        _write(
            tmp_path / "analysis" / f"{d}.json",
            {"selected_for_implementation": [], "rejected": []},
        )
        _write(tmp_path / "integration" / f"{d}.json", {"merged": [], "skipped": []})

        with caplog.at_level(logging.WARNING, logger="evolution_funnel"):
            r = compute_funnel(tmp_path, d)

        assert r["issues_created"] == 2
        assert "legacy key 'proposals_filed'" in caplog.text
        assert "2026-07-05" in caplog.text


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


class TestSummary:
    def _rec(self, date, created=0, selected=0, rejected=0, merged=0, skipped=0):
        return {
            "date": date,
            "issues_created": created,
            "selected": selected,
            "rejected": rejected,
            "merged": merged,
            "skipped": skipped,
        }

    def test_load_records_skips_blank_and_malformed(self, tmp_path):
        f = tmp_path / "metrics.jsonl"
        f.write_text(
            json.dumps(self._rec("2026-06-10", merged=1))
            + "\n\nnot-json\n"
            + json.dumps(self._rec("2026-06-11", merged=2))
            + "\n",
            encoding="utf-8",
        )
        recs = load_records(f)
        assert [r["date"] for r in recs] == ["2026-06-10", "2026-06-11"]

    def test_load_records_missing_file(self, tmp_path):
        assert load_records(tmp_path / "nope.jsonl") == []

    def test_reject_rate_and_window(self):
        recs = [self._rec(f"d{i}", selected=1, rejected=9, merged=1) for i in range(10)]
        s = summarize(recs, last=7)
        assert s["cycles"] == 7  # window honored
        assert s["selected"] == 7 and s["rejected"] == 63
        assert s["reject_rate"] == round(63 / 70, 3)  # 0.9
        assert any("HIGH_REJECT_RATE" in f for f in s["flags"])

    def test_healthy_signal_no_flags(self):
        recs = [self._rec(f"d{i}", selected=8, rejected=2, merged=3) for i in range(5)]
        s = summarize(recs, last=7)
        assert s["reject_rate"] == 0.2
        assert s["flags"] == []
        assert "signal OK" in format_summary(s)

    def test_merged_zero_streak_flag(self):
        recs = [
            self._rec("d1", merged=2),
            self._rec("d2", merged=0),
            self._rec("d3", merged=0),
            self._rec("d4", merged=0),
        ]
        s = summarize(recs, last=7)
        assert s["merged_zero_streak"] == 3
        assert any("MERGED_ZERO" in f for f in s["flags"])

    def test_empty_records_no_crash(self):
        s = summarize([], last=7)
        assert s["cycles"] == 0 and s["reject_rate"] == 0.0 and s["flags"] == []
        assert "[evolution-funnel]" in format_summary(s)


class TestSummarySidecar:
    def test_normal_run_writes_summary_sidecar(self, tmp_path, monkeypatch):
        # evolution-research has no terminal toolset, so the no_agent funnel run
        # must leave a file it can read. A normal main() run writes it.
        monkeypatch.setenv("EVOLUTION_PROFILE_DIR", str(tmp_path))
        (tmp_path / "metrics.jsonl").write_text(
            json.dumps({
                "date": "2026-06-10",
                "selected": 1,
                "rejected": 9,
                "merged": 0,
            })
            + "\n",
            encoding="utf-8",
        )
        from evolution_funnel import main

        rc = main(["evolution_funnel.py", "2026-06-11"])
        assert rc == 0
        sidecar = tmp_path / "funnel-summary.txt"
        assert sidecar.exists()
        body = sidecar.read_text()
        assert body.startswith("[evolution-funnel] last")
        # the seeded 90% reject cycle should surface the directive
        assert "HIGH_REJECT_RATE" in body
