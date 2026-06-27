"""Tests for scripts/evolution_postmortem_miner.py (#578)."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

# noqa: E402
from evolution_postmortem_miner import (  # type: ignore[import-not-found]
    _classify_close_reason,
    _extract_pattern,
    _make_rule_id,
    extract_rules_from_pr,
    load_existing_rules,
    merge_rules,
)


class TestClassifyCloseReason:
    def test_merged_label_returns_merged(self):
        assert _classify_close_reason(["merged", "bug"], "closed") == "merged"

    def test_merged_state_returns_merged(self):
        assert _classify_close_reason([], "merged") == "merged"

    def test_implemented_on_main(self):
        assert (
            _classify_close_reason(["implemented-on-main"], "closed")
            == "implemented-on-main"
        )

    def test_duplicate_first(self):
        assert _classify_close_reason(["duplicate", "rejected"], "closed") == "duplicate"

    def test_needs_work_second(self):
        assert _classify_close_reason(["needs-work"], "closed") == "needs-work"

    def test_rejected_fallback(self):
        assert _classify_close_reason(["rejected"], "closed") == "rejected"

    def test_no_labels_closed_is_rejected(self):
        assert _classify_close_reason([], "closed") == "rejected"


class TestExtractPattern:
    def test_title_no_labels(self):
        pr = {"title": "Fix terminal timeout", "labels": []}
        assert _extract_pattern(pr) == "Fix terminal timeout"

    def test_title_with_fix_label(self):
        pr = {"title": "Terminal timeout", "labels": [{"name": "fix"}]}
        assert _extract_pattern(pr) == "[fix] Terminal timeout"

    def test_no_double_prefix(self):
        pr = {"title": "[fix] Terminal timeout", "labels": [{"name": "fix"}]}
        assert _extract_pattern(pr) == "[fix] Terminal timeout"

    def test_empty_title(self):
        pr = {"title": "", "labels": [{"name": "fix"}]}
        assert _extract_pattern(pr) == "[fix]"


class TestMakeRuleId:
    def test_format(self):
        rid = _make_rule_id(42, 1)
        assert rid.startswith("rule-")
        assert "-0042-001" in rid


class TestExtractRulesFromPr:
    def test_merged_pr_no_rules(self):
        pr = {"number": 1, "title": "Something", "labels": [{"name": "merged"}], "state": "closed"}
        rules = extract_rules_from_pr(pr, set())
        assert rules == []

    def test_duplicate_pr_no_rules(self):
        pr = {"number": 1, "title": "Something", "labels": [{"name": "duplicate"}], "state": "closed"}
        rules = extract_rules_from_pr(pr, set())
        assert rules == []

    def test_rejected_pr_produces_rule(self):
        pr = {
            "number": 42,
            "title": "Add new tool",
            "labels": [{"name": "rejected"}],
            "state": "closed",
        }
        rules = extract_rules_from_pr(pr, set())
        assert len(rules) == 1
        r = rules[0]
        assert r["source_pr"] == 42
        assert r["close_reason"] == "rejected"
        assert "pattern" in r
        assert "created" in r
        assert r["hit_count"] == 0

    def test_existing_id_skipped(self):
        pr = {
            "number": 42,
            "title": "Fix bug",
            "labels": [{"name": "rejected"}],
            "state": "closed",
        }
        # First call generates rule
        rules1 = extract_rules_from_pr(pr, set())
        rid = rules1[0]["id"]
        # Second call with the existing ID skips
        rules2 = extract_rules_from_pr(pr, {rid})
        assert rules2 == []


class TestLoadExistingRules:
    def test_missing_file_returns_skeleton(self, tmp_path):
        data = load_existing_rules(tmp_path / "nonexistent.json")
        assert data == {"rules": [], "last_scanned_pr": 0, "stats": {}}

    def test_valid_file(self, tmp_path):
        f = tmp_path / "rules.json"
        f.write_text(json.dumps({"rules": [], "last_scanned_pr": 5, "stats": {}}))
        data = load_existing_rules(f)
        assert data["last_scanned_pr"] == 5

    def test_corrupt_file_returns_skeleton(self, tmp_path):
        f = tmp_path / "bad.json"
        f.write_text("not json")
        data = load_existing_rules(f)
        assert data == {"rules": [], "last_scanned_pr": 0, "stats": {}}


class TestMergeRules:
    def test_dedup_by_id(self):
        existing = {"rules": [{"id": "rule-001", "pattern": "old", "source_pr": 1,
                                "close_reason": "rejected", "created": "", "hit_count": 0}],
                    "last_scanned_pr": 0, "stats": {}}
        new = [{"id": "rule-001", "pattern": "old", "source_pr": 1,
                "close_reason": "rejected", "created": "", "hit_count": 0},
               {"id": "rule-002", "pattern": "new", "source_pr": 2,
                "close_reason": "needs-work", "created": "", "hit_count": 0}]
        out = merge_rules(existing, new, last_scanned_pr=2, total_scanned=5, skipped_merged=3)
        assert out["stats"]["new_rules"] == 1
        assert out["stats"]["total_rules"] == 2
        assert out["stats"]["total_scanned"] == 5
        assert out["stats"]["skipped_merged"] == 3
        assert out["last_scanned_pr"] == 2

    def test_all_new(self):
        existing = {"rules": [], "last_scanned_pr": 0, "stats": {}}
        new = [{"id": "rule-001", "pattern": "check: Fix bug", "source_pr": 1,
                "close_reason": "rejected", "created": "2026-01-01T00:00:00Z", "hit_count": 0}]
        out = merge_rules(existing, new, last_scanned_pr=1, total_scanned=1, skipped_merged=0)
        assert out["stats"]["new_rules"] == 1
        assert out["stats"]["total_rules"] == 1
        assert out["rules"][0]["source_pr"] == 1
