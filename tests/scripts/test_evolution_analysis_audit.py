"""Tests for scripts/evolution_analysis_audit.py — deterministic selection-budget
enforcement (the teeth behind PR #519's prompt-level effort-budget contract)."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from evolution_analysis_audit import (  # noqa: E402
    audit_analysis,
    audit_latest,
    audit_rejections,
)


def _report(max_total_effort=None, total_effort_selected=None, top_level=False):
    sc = {"min_priority": 0.7, "max_items": 5}
    if max_total_effort is not None:
        sc["max_total_effort"] = max_total_effort
    report = {"date": "2026-06-24"}
    if total_effort_selected is not None:
        report["total_effort_selected"] = total_effort_selected
    if top_level:
        report["selection_constraints"] = sc
    else:
        report["scoring_model"] = {"base_formula": "...", "selection_constraints": sc}
    return report


class TestAuditAnalysis:
    def test_legal_default_budget_is_clean(self):
        assert audit_analysis(_report(3.0, 2.0)) == []

    def test_legal_throttled_budget_is_clean(self):
        assert audit_analysis(_report(1.5, 1.5)) == []

    def test_illegal_middle_budget_flagged(self):
        # The exact 2026-06-24 defect: 2.0 is neither 1.5 nor 3.0.
        v = audit_analysis(_report(2.0, 2.0))
        assert any("BUDGET_ILLEGAL" in x and "2" in x for x in v)

    def test_overspent_flagged(self):
        v = audit_analysis(_report(1.5, 2.5))
        assert any("BUDGET_OVERSPENT" in x for x in v)

    def test_illegal_and_overspent_both_flagged(self):
        v = audit_analysis(_report(2.0, 2.5))
        assert any("BUDGET_ILLEGAL" in x for x in v)
        assert any("BUDGET_OVERSPENT" in x for x in v)

    def test_spent_equal_to_budget_is_clean(self):
        assert audit_analysis(_report(3.0, 3.0)) == []

    def test_top_level_selection_constraints_fallback(self):
        assert audit_analysis(_report(2.0, top_level=True))  # still flagged
        assert audit_analysis(_report(3.0, 3.0, top_level=True)) == []

    def test_missing_budget_is_not_flagged(self):
        # No max_total_effort at all → skip (no false alarm on a partial report).
        assert audit_analysis(_report(None, 2.0)) == []

    def test_missing_spent_skips_overspent_only(self):
        # Illegal budget still flagged; overspent can't be evaluated → not flagged.
        v = audit_analysis(_report(2.0, None))
        assert any("BUDGET_ILLEGAL" in x for x in v)
        assert not any("BUDGET_OVERSPENT" in x for x in v)

    def test_bool_budget_is_ignored(self):
        # True is an int in Python; must not be read as 1.0 and flagged.
        assert audit_analysis(_report(True, 1.5)) == []

    def test_non_dict_report_is_safe(self):
        assert audit_analysis([]) == []  # type: ignore[arg-type]
        assert audit_analysis("nope") == []  # type: ignore[arg-type]

    def test_custom_legal_budgets(self):
        assert audit_analysis(_report(2.0, 2.0), legal_budgets=(2.0,)) == []


class TestAuditLatest:
    def _write(self, d, name, report):
        (d / "analysis").mkdir(parents=True, exist_ok=True)
        (d / "analysis" / name).write_text(json.dumps(report), encoding="utf-8")

    def test_audits_latest_dated_report(self, tmp_path):
        self._write(tmp_path, "2026-06-23.json", _report(3.0, 2.0))  # clean, older
        self._write(tmp_path, "2026-06-24.json", _report(2.0, 2.0))  # illegal, newer
        out = audit_latest(tmp_path)
        assert len(out) == 1
        assert "2026-06-24" in out[0] and "BUDGET_ILLEGAL" in out[0]

    def test_ignores_non_dated_snapshots(self, tmp_path):
        # issues_*.json / prs_*.json must not be parsed as cycle reports.
        self._write(tmp_path, "issues_2026-06-24.json", {"junk": True})
        self._write(tmp_path, "prs_2026-06-24.json", [])
        self._write(tmp_path, "2026-06-24.json", _report(3.0, 2.0))
        assert audit_latest(tmp_path) == []

    def test_no_reports_is_silent(self, tmp_path):
        assert audit_latest(tmp_path) == []

    def test_unreadable_json_is_silent(self, tmp_path):
        (tmp_path / "analysis").mkdir(parents=True)
        (tmp_path / "analysis" / "2026-06-24.json").write_text("{not json", encoding="utf-8")
        assert audit_latest(tmp_path) == []

    def test_audit_latest_runs_rejection_check_with_repo(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        self._write(
            tmp_path,
            "2026-06-24.json",
            {
                "scoring_model": {"selection_constraints": {"max_total_effort": 3.0}},
                "total_effort_selected": 2.0,
                "rejected": [
                    {
                        "issue_number": 83,
                        "reason_code": "already-exists",
                        "reason": "already done in scripts/ghost.sh",
                        "closed": True,
                    }
                ],
            },
        )
        out = audit_latest(tmp_path, repo)
        assert any("FABRICATED_REJECTION" in x and "#83" in x for x in out)

    def test_audit_latest_without_repo_skips_rejection_check(self, tmp_path):
        self._write(
            tmp_path,
            "2026-06-24.json",
            {
                "scoring_model": {"selection_constraints": {"max_total_effort": 3.0}},
                "total_effort_selected": 2.0,
                "rejected": [
                    {
                        "issue_number": 83,
                        "reason_code": "already-exists",
                        "reason": "already done in scripts/ghost.sh",
                        "closed": True,
                    }
                ],
            },
        )
        assert audit_latest(tmp_path) == []  # no repo_root → no fabrication check


class TestAuditRejections:
    def _rep(self, rejected):
        return {"date": "2026-06-24", "rejected": rejected}

    def test_already_exists_with_real_path_is_clean(self, tmp_path):
        (tmp_path / "agent").mkdir()
        (tmp_path / "agent" / "real.py").write_text("x")
        rej = [
            {
                "issue_number": 1,
                "reason_code": "already-exists",
                "reason": "already implemented in agent/real.py",
                "closed": True,
            }
        ]
        assert audit_rejections(self._rep(rej), tmp_path) == []

    def test_fabricated_path_flagged(self, tmp_path):
        # The real #83: cited scripts/evolution_watchdog.sh (the actual one is .py).
        rej = [
            {
                "issue_number": 83,
                "reason_code": "already-exists",
                "reason": "already done in scripts/evolution_watchdog.sh and skills/x/SKILL.md",
                "closed": True,
            }
        ]
        v = audit_rejections(self._rep(rej), tmp_path)
        assert any("FABRICATED_REJECTION" in x and "#83" in x for x in v)

    def test_mixed_real_and_missing_is_not_flagged(self, tmp_path):
        (tmp_path / "agent").mkdir()
        (tmp_path / "agent" / "real.py").write_text("x")
        rej = [
            {
                "issue_number": 5,
                "reason_code": "already-exists",
                "reason": "see agent/real.py and agent/typoed_missing.py",
                "closed": True,
            }
        ]
        assert audit_rejections(self._rep(rej), tmp_path) == []

    def test_other_reason_codes_ignored(self, tmp_path):
        rej = [
            {
                "issue_number": 7,
                "reason_code": "harmful",
                "reason": "mentions nonexistent/path.py but is not an already-exists claim",
                "closed": True,
            }
        ]
        assert audit_rejections(self._rep(rej), tmp_path) == []

    def test_no_concrete_path_is_not_flagged(self, tmp_path):
        rej = [
            {
                "issue_number": 9,
                "reason_code": "already-exists",
                "reason": "this capability already exists in the codebase",
                "closed": True,
            }
        ]
        assert audit_rejections(self._rep(rej), tmp_path) == []

    def test_path_with_line_numbers_extracts_cleanly(self, tmp_path):
        rej = [
            {
                "issue_number": 11,
                "reason_code": "already-exists",
                "reason": "implemented in tools/missing_tool.py (lines 53-54, 682+)",
                "closed": True,
            }
        ]
        v = audit_rejections(self._rep(rej), tmp_path)
        assert any("tools/missing_tool.py" in x for x in v)

    def test_no_repo_root_is_silent(self):
        rej = [{"issue_number": 1, "reason_code": "already-exists", "reason": "x/y.py"}]
        assert audit_rejections(self._rep(rej), None) == []

    def test_empty_or_missing_rejections_clean(self, tmp_path):
        assert audit_rejections({"rejected": []}, tmp_path) == []
        assert audit_rejections({}, tmp_path) == []

