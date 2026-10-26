"""Tests for the post-hoc claim audit (#3521)."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from evolution.lib.source_claim_audit import (
    audit_report,
    extract_figures,
    load_sources,
    render_summary,
)

URL = "https://arxiv.org/abs/2610.31372"

_SOURCE_TEXT = (
    "RIDAR defines a 14-type taxonomy of research-integrity violations, reports "
    "85.2% F1 on a 60-trajectory benchmark, and finds violations in 99.7% of 300 "
    "wild trajectories. Submitted 2026-10-22."
)

_REPORT = f"""# Research Report - 2026-10-26

### [IMPROVEMENT] Post-hoc claim audit
- **Source**: {URL}
- **Priority Score**: 1.44 (impact 0.9)

The paper reports 85.2% F1 on a 60-trajectory benchmark and was submitted
on 2026-10-22. It counts 99.7% of 300 wild trajectories as violating.
"""


def test_figures_extracted_and_dates_not_double_counted():
    figures = extract_figures(f"- **Source**: {URL} (2026-10-22)")
    assert figures == [("2026-10-22", "date")]
    # A count noun makes an integer a claim; a section/step index does not.
    assert extract_figures("seeded with 7 skills") == [("7", "number")]
    assert extract_figures("step 7 of the run") == []
    # Our own artefacts and the citation itself are not facts from the source.
    assert extract_figures("fixed in issue #3514 and v2 of the tool") == []
    assert extract_figures("latency dropped 412.5 ms") == [("412.5", "number")]


def test_grounded_figures_are_verified():
    result = audit_report(_REPORT, {URL: _SOURCE_TEXT})
    assert result.skipped is None
    assert result.passed is True
    assert result.unverified == []
    assert {c.figure for c in result.verified} >= {
        "85.2",
        "60",
        "99.7",
        "300",
        "2026-10-22",
    }


def test_altered_count_is_flagged_as_unverified_and_fails_the_gate():
    tampered = _REPORT.replace("85.2%", "85.7%")
    result = audit_report(tampered, {URL: _SOURCE_TEXT})
    assert result.passed is False
    assert [c.figure for c in result.unverified] == ["85.7"]
    assert result.unverified[0].url == URL


def test_date_matches_any_surface_form_of_the_source():
    report = f"- **Source**: {URL} (submitted 2026-10-22)"
    result = audit_report(report, {URL: "Submitted October 22, 2026 to arXiv."})
    assert [(c.figure, c.verdict) for c in result.claims] == [
        ("2026-10-22", "verified")
    ]


def test_longer_number_does_not_satisfy_a_shorter_claim():
    report = f"- **Source**: {URL} reports 23% of runs."
    result = audit_report(report, {URL: "reports 1234 runs"})
    assert [c.verdict for c in result.claims] == ["unverified"]


def test_thousands_separator_is_not_a_mismatch():
    report = f"- **Source**: {URL} covers 19,559 files."
    result = audit_report(report, {URL: "the checkout holds 19559 files"})
    assert [c.verdict for c in result.claims] == ["verified"]


def test_figure_without_citation_is_unattributed_but_passes():
    result = audit_report("Latency improved 12.5% overall.", {URL: _SOURCE_TEXT})
    assert [c.verdict for c in result.claims] == ["unattributed"]
    assert result.passed is True


def test_cited_url_missing_from_sidecar_is_unattributed_not_fabricated():
    report = "- **Source**: https://example.invalid/x reports 12.5% growth"
    result = audit_report(report, {URL: _SOURCE_TEXT})
    assert [c.verdict for c in result.claims] == ["unattributed"]
    assert result.passed is True


def test_missing_sidecar_skips_instead_of_blocking_the_stage():
    result = audit_report(_REPORT, {})
    assert result.skipped == "no evidence sidecar"
    assert result.claims == []
    assert "skipped" in render_summary(result)


def test_load_sources_accepts_object_and_list_and_bad_input(tmp_path):
    obj = tmp_path / "obj.json"
    obj.write_text('{"https://a.b": "text"}', encoding="utf-8")
    assert load_sources(obj) == {"https://a.b": "text"}
    lst = tmp_path / "list.json"
    lst.write_text('[{"url": "https://c.d", "text": "more"}]', encoding="utf-8")
    assert load_sources(lst) == {"https://c.d": "more"}
    bad = tmp_path / "bad.json"
    bad.write_text("not json", encoding="utf-8")
    assert load_sources(bad) == {}
    assert load_sources(tmp_path / "absent.json") == {}
