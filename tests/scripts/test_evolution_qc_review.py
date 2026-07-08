"""Tests for scripts/evolution_qc_review.py — agentic QC review (#796)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from evolution_qc_review import QC_CATEGORIES, build_qc_review_task, parse_qc_report  # noqa: E402


class TestBuild:
    def test_shape_and_content(self):
        t = build_qc_review_task("Implemented X", ["scripts/x.py"])
        assert t["role"] == "leaf" and t["toolsets"] == ["file"]
        assert "Implemented X" in t["goal"] and "VERDICT: PASS" in t["goal"]

    def test_files_and_issue_in_goal(self):
        t = build_qc_review_task(
            "s", ["a.py", "b.py"], issue_number=796, issue_title="QC"
        )
        assert "a.py" in t["goal"] and "#796" in t["goal"] and "QC" in t["goal"]

    def test_no_issue_and_empty_files(self):
        t = build_qc_review_task("s", [])
        assert "#796" not in t["goal"] and "(none specified)" in t["goal"]

    def test_all_categories_in_checklist(self):
        t = build_qc_review_task("s", ["f"])
        for cat in QC_CATEGORIES:
            assert cat.replace("_", " ").title() in t["goal"]

    def test_custom_toolsets(self):
        assert build_qc_review_task("s", ["f"], toolsets=["web"])["toolsets"] == ["web"]


class TestParse:
    def test_explicit_verdicts(self):
        assert parse_qc_report("VERDICT: PASS\nAll clean.")["verdict"] == "pass"
        assert parse_qc_report("VERDICT: FAIL\nBad.")["verdict"] == "fail"

    def test_keyword_verdicts(self):
        assert parse_qc_report("LGTM, no issues.")["verdict"] == "pass"
        assert parse_qc_report("This failed review.")["verdict"] == "fail"

    def test_unknown_and_empty(self):
        assert parse_qc_report("random text")["verdict"] == "unknown"
        r = parse_qc_report("")
        assert r["verdict"] == "unknown" and r["findings"] == []

    def test_explicit_wins_over_keywords(self):
        assert (
            parse_qc_report("VERDICT: PASS\nTest failed but ok.")["verdict"] == "pass"
        )

    def test_findings_and_blocking(self):
        r = parse_qc_report(
            "VERDICT: FAIL\n[security] [critical] — secret\n[correctness] [medium] — bug"
        )
        assert (
            len(r["findings"]) == 2 and r["blocking_count"] == 1 and r["has_blocking"]
        )

    def test_space_in_category(self):
        r = parse_qc_report("VERDICT: FAIL\n[test coverage] [low] — gap")
        assert r["findings"][0]["category"] == "test_coverage"

    def test_unknown_cat_sev_ignored(self):
        assert (
            len(parse_qc_report("VERDICT: FAIL\n[perf] [high] — slow")["findings"]) == 0
        )
        assert (
            len(
                parse_qc_report("VERDICT: FAIL\n[security] [blocker] — bad")["findings"]
            )
            == 0
        )

    def test_no_blocking_when_low(self):
        r = parse_qc_report("VERDICT: PASS\n[test_coverage] [low] — minor")
        assert r["blocking_count"] == 0 and not r["has_blocking"]
