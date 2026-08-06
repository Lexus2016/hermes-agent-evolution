"""Tests for evolution_pr_reflection — PR feedback signal (#1584).

Tests the pure functions (reflect, format_sidecar, _classify_pr,
extract_reject_reason) with injected PR data — no ``gh`` calls.
"""

from __future__ import annotations

import json
from pathlib import Path

from scripts.evolution_pr_reflection import (
    _classify_pr,
    extract_reject_reason,
    format_sidecar,
    reflect,
    run_reflection,
)


class TestClassifyPR:
    def test_merged_pr(self):
        pr = {"mergedAt": "2026-08-06T10:00:00Z", "headRefName": "evolution/issue-123"}
        assert _classify_pr(pr) == "merged"

    def test_rejected_evolution_pr(self):
        pr = {"mergedAt": None, "headRefName": "evolution/issue-456"}
        assert _classify_pr(pr) == "rejected"

    def test_closed_other(self):
        pr = {"mergedAt": None, "headRefName": "feature/random"}
        assert _classify_pr(pr) == "closed-other"

    def test_merged_takes_priority_over_rejected(self):
        pr = {"mergedAt": "2026-08-06T10:00:00Z", "headRefName": "evolution/x"}
        assert _classify_pr(pr) == "merged"


class TestExtractRejectReason:
    def test_out_of_scope(self):
        pr = {"body": "Skipped at implementation: out-of-scope", "title": ""}
        assert extract_reject_reason(pr) == "out-of-scope"

    def test_already_exists(self):
        pr = {"body": "", "title": "Skipped: already-exists in context.py"}
        assert extract_reject_reason(pr) == "already-exists"

    def test_ci_green_failure(self):
        pr = {"body": "could not get CI green — ruff failed", "title": ""}
        assert extract_reject_reason(pr) == "could not get ci green"

    def test_no_reason(self):
        pr = {"body": "Just a normal PR", "title": "feat: something"}
        assert extract_reject_reason(pr) is None

    def test_needs_decomposition(self):
        pr = {"body": "needs-decomposition — too large", "title": ""}
        assert extract_reject_reason(pr) == "needs-decomposition"


class TestReflect:
    def test_empty_prs(self):
        result = reflect([])
        assert result["total"] == 0
        assert result["merged"] == 0
        assert result["merge_rate"] is None
        assert result["patterns"] == []

    def test_all_merged(self):
        prs = [
            {"mergedAt": "2026-08-06T10:00:00Z", "headRefName": "evolution/a"},
            {"mergedAt": "2026-08-06T11:00:00Z", "headRefName": "evolution/b"},
        ]
        result = reflect(prs)
        assert result["merged"] == 2
        assert result["merge_rate"] == 1.0
        assert "High merge rate" in result["patterns"][0]

    def test_low_merge_rate_pattern(self):
        prs = [
            {
                "mergedAt": None,
                "headRefName": "evolution/a",
                "body": "out-of-scope",
                "title": "",
            },
            {
                "mergedAt": None,
                "headRefName": "evolution/b",
                "body": "out-of-scope",
                "title": "",
            },
            {"mergedAt": "2026-08-06T10:00:00Z", "headRefName": "evolution/c"},
        ]
        result = reflect(prs)
        assert result["merge_rate"] is not None
        assert result["merge_rate"] < 0.4
        assert any("Low merge rate" in p for p in result["patterns"])
        assert any("out-of-scope" in p for p in result["patterns"])

    def test_repeat_rejection_pattern(self):
        prs = [
            {
                "mergedAt": None,
                "headRefName": "evolution/a",
                "body": "harmful change",
                "title": "",
            },
            {
                "mergedAt": None,
                "headRefName": "evolution/b",
                "body": "harmful change",
                "title": "",
            },
            {
                "mergedAt": None,
                "headRefName": "evolution/c",
                "body": "harmful change",
                "title": "",
            },
            {"mergedAt": "2026-08-06T10:00:00Z", "headRefName": "evolution/d"},
        ]
        result = reflect(prs)
        assert any("harmful" in p for p in result["patterns"])

    def test_closed_other_not_counted_as_rejected(self):
        prs = [
            {
                "mergedAt": None,
                "headRefName": "dependabot/update",
                "body": "",
                "title": "",
            },
        ]
        result = reflect(prs)
        assert result["rejected"] == 0


class TestFormatSidecar:
    def test_basic_format(self):
        h = {
            "total": 5,
            "merged": 3,
            "rejected": 2,
            "reject_reasons": {"out-of-scope": 2},
            "merge_rate": 0.6,
            "patterns": ["Test pattern"],
        }
        line = format_sidecar(h)
        assert "[evolution-pr-reflection]" in line
        assert "closed=5" in line
        assert "merged=3" in line
        assert "rejected=2" in line
        assert "merge_rate=60%" in line
        assert "out-of-scope=2" in line
        assert "Test pattern" in line

    def test_empty_reasons(self):
        h = {
            "total": 2,
            "merged": 2,
            "rejected": 0,
            "reject_reasons": {},
            "merge_rate": 1.0,
            "patterns": [],
        }
        line = format_sidecar(h)
        assert "reject_reasons=none" in line
        assert "no patterns" in line

    def test_null_merge_rate(self):
        h = {
            "total": 0,
            "merged": 0,
            "rejected": 0,
            "reject_reasons": {},
            "merge_rate": None,
            "patterns": [],
        }
        line = format_sidecar(h)
        assert "merge_rate=n/a" in line


class TestRunReflection:
    def test_writes_sidecar(self, tmp_path):
        prs = [
            {"mergedAt": "2026-08-06T10:00:00Z", "headRefName": "evolution/a"},
        ]
        result = run_reflection(evolution_dir=tmp_path, prs=prs)
        assert result["sidecar_written"] is True
        sidecar = (tmp_path / "pr-reflection.txt").read_text()
        assert "[evolution-pr-reflection]" in sidecar

    def test_injected_prs_no_gh_call(self, tmp_path):
        prs = [
            {
                "mergedAt": None,
                "headRefName": "evolution/x",
                "body": "duplicate",
                "title": "",
            },
        ]
        result = run_reflection(evolution_dir=tmp_path, prs=prs)
        assert result["total"] == 1
        assert result["rejected"] == 1
        assert result["reject_reasons"] == {"duplicate": 1}
