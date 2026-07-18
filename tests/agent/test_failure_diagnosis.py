# -*- coding: utf-8 -*-
"""Unit tests for multi-hypothesis failure diagnosis (#1029)."""

from __future__ import annotations

from tools.tool_failure_classifier import ToolFailureCategory, matched_categories
from agent.failure_diagnosis import (
    DIAGNOSIS_GUIDANCE_PREFIX,
    Diagnosis,
    DiagnosisMode,
    Hypothesis,
    HypothesisHistory,
    diagnose_failure,
    maybe_append_diagnosis,
)


def test_matched_categories_surfaces_multiple_plausible_categories():
    # "not found" alone -> not_found; but "command not found" -> tool_unavailable
    cats = matched_categories("bash: command not found; also file does not exist")
    assert ToolFailureCategory.tool_unavailable in cats
    assert ToolFailureCategory.not_found in cats
    # ordered + deduped
    assert len(cats) == len(set(cats))


def test_matched_categories_empty_for_unmatched_text():
    assert matched_categories("") == []


def test_diagnose_primary_hypothesis_matches_classifier():
    diag = diagnose_failure("web_search", "429 Too Many Requests")
    assert diag.primary is not None
    assert diag.primary.category is ToolFailureCategory.rate_limited
    assert diag.primary.rank == 1
    assert diag.primary.confidence == 0.7


def test_diagnose_produces_ranked_multiple_hypotheses():
    diag = diagnose_failure("terminal", "command not found: foo; no such file or directory")
    assert len(diag.hypotheses) >= 2
    ranks = [h.rank for h in diag.hypotheses]
    assert ranks == sorted(ranks)  # strictly ranked 1..n
    confs = [h.confidence for h in diag.hypotheses]
    assert confs == sorted(confs, reverse=True)  # decreasing confidence


def test_diagnose_respects_max_hypotheses():
    diag = diagnose_failure(
        "terminal",
        "command not found; no such file; permission denied; rate limit; timed out",
        max_hypotheses=2,
    )
    assert len(diag.hypotheses) <= 2


def test_diagnose_dedups_categories():
    diag = diagnose_failure("web_search", "not found and does not exist and no such thing")
    cats = [h.category for h in diag.hypotheses]
    assert len(cats) == len(set(cats))


def test_diagnosis_mode_coerce():
    assert DiagnosisMode.coerce("off") is DiagnosisMode.off
    assert DiagnosisMode.coerce("reflect") is DiagnosisMode.reflect
    assert DiagnosisMode.coerce("multi-hypothesis") is DiagnosisMode.multi_hypothesis
    assert DiagnosisMode.coerce("multi_hypothesis") is DiagnosisMode.multi_hypothesis
    assert DiagnosisMode.coerce("garbage") is DiagnosisMode.off
    assert DiagnosisMode.coerce(None) is DiagnosisMode.off


def test_diagnosis_to_dict_shape():
    diag = diagnose_failure("web_search", "429 rate limit")
    data = diag.to_dict()
    assert data["tool_name"] == "web_search"
    assert data["hypotheses"][0]["category"] == "rate_limited"
    assert data["hypotheses"][0]["rank"] == 1


# --- seam ---------------------------------------------------------------------


def test_seam_off_returns_unchanged():
    r = '{"error": "429 rate limit"}'
    assert maybe_append_diagnosis(r, "web_search", failed=True, mode="off") == r


def test_seam_not_failed_returns_unchanged():
    r = '{"ok": true}'
    assert maybe_append_diagnosis(r, "web_search", failed=False, mode="multi-hypothesis") == r


def test_seam_reflect_appends_single_hypothesis():
    r = '{"error": "command not found; no such file"}'
    out = maybe_append_diagnosis(r, "terminal", failed=True, mode="reflect")
    assert DIAGNOSIS_GUIDANCE_PREFIX in out
    # reflect => exactly one ranked entry "1)" and no "2)"
    assert "1)" in out
    assert "2)" not in out


def test_seam_multi_hypothesis_appends_ranked_list():
    r = '{"error": "command not found; no such file or directory"}'
    out = maybe_append_diagnosis(r, "terminal", failed=True, mode="multi-hypothesis")
    assert DIAGNOSIS_GUIDANCE_PREFIX in out
    assert "1)" in out
    assert "2)" in out


def test_seam_never_raises():
    out = maybe_append_diagnosis("\x00", "terminal", failed=True, mode="multi-hypothesis")
    assert out.startswith("\x00")


def test_seam_none_result():
    out = maybe_append_diagnosis(None, "web_search", failed=True, mode="reflect")
    assert isinstance(out, str)


# --- #1030: hypothesis history --------------------------------------------------


def test_history_records_and_reports_tried():
    h = HypothesisHistory()
    assert h.tried("k") == ()
    h.record("k", ToolFailureCategory.rate_limited)
    h.record("k", ToolFailureCategory.rate_limited)  # dedup
    assert h.tried("k") == (ToolFailureCategory.rate_limited,)
    h.record("k", ToolFailureCategory.timeout)
    assert set(h.tried("k")) == {ToolFailureCategory.rate_limited, ToolFailureCategory.timeout}


def test_history_reset_and_to_dict():
    h = HypothesisHistory()
    h.record("k", ToolFailureCategory.not_found)
    assert h.to_dict() == {"k": ["not_found"]}
    h.reset()
    assert h.to_dict() == {}


def test_diagnose_records_primary_into_history():
    h = HypothesisHistory()
    diag = diagnose_failure("web_search", "429 rate limit", history=h, history_key="web_search:x")
    assert diag.primary.category is ToolFailureCategory.rate_limited
    assert ToolFailureCategory.rate_limited in h.tried("web_search:x")


def test_repeated_diagnosis_demotes_tried_hypothesis():
    h = HypothesisHistory()
    # Two plausible categories in the text: tool_unavailable + not_found.
    err = "command not found: foo; also no such file or directory"
    first = diagnose_failure("terminal", err, history=h, history_key="terminal:x")
    first_primary = first.primary.category
    # On the repeat, the previously-surfaced primary is demoted so a fresh
    # hypothesis rises to rank 1 (as long as another category matched).
    second = diagnose_failure("terminal", err, history=h, history_key="terminal:x")
    assert second.primary.category is not first_primary
    assert second.primary.category in matched_categories(err)


def test_history_key_is_isolated():
    h = HypothesisHistory()
    err = "429 rate limit"
    diagnose_failure("web_search", err, history=h, history_key="web_search:a")
    # A different key is unaffected — same primary as a fresh diagnosis.
    other = diagnose_failure("web_search", err, history=h, history_key="web_search:b")
    assert other.primary.category is ToolFailureCategory.rate_limited


def test_seam_threads_history_through():
    h = HypothesisHistory()
    err = '{"error": "command not found; no such file or directory"}'
    out1 = maybe_append_diagnosis(err, "terminal", failed=True, mode="reflect", history=h, history_key="terminal:x")
    out2 = maybe_append_diagnosis(err, "terminal", failed=True, mode="reflect", history=h, history_key="terminal:x")
    assert DIAGNOSIS_GUIDANCE_PREFIX in out1
    # The two reflections should differ because the first hypothesis was demoted.
    assert out1 != out2
