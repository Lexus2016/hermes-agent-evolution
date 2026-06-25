"""Tests for scripts/evolution_analysis_audit.py — deterministic selection-budget
enforcement (the teeth behind PR #519's prompt-level effort-budget contract)."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from evolution_analysis_audit import audit_analysis, audit_latest  # noqa: E402


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
