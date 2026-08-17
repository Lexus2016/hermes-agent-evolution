"""Tests for deterministic claim extraction (#2482 Slice A / #2513).

Slice A requires that the rubric-judge output includes a structured list of
extracted claims (not only an aggregate score) and that extraction is
deterministic / reproducible for identical input.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from evolution_rubric_judge import (  # noqa: E402
    CLAIM_VERDICTS,
    StrictRubricJudgeGrader,
    _markdown_sections,
    assess_stat_validity,
    assess_stat_validity_claims,
    compute_claim_score,
    compute_stat_validity_stats,
    detect_rejection_bias,
    extract_claims,
    triage_claim,
    triage_claims,
)

SAMPLE = """\
# Finding 1
Adopting the parallel compactor improved merge latency by 52% across the
benchmark suite. Source: https://example.com/bench.

# Finding 2
The tokenizer change reduced prompt tokens by 64%, cutting cost per run.
Some neutral background prose with no measurable outcome here.
"""


def test_markdown_sections_keeps_order_and_preamble() -> None:
    assert [h for h, _ in _markdown_sections(SAMPLE)] == ["Finding 1", "Finding 2"]
    blocks = _markdown_sections("lead-in prose\n\n# Head\nbody text")
    assert blocks[0][0] == "" and blocks[1][0] == "Head"


def test_extract_claims_emits_structured_list() -> None:
    claims = extract_claims(("research", SAMPLE))
    assert claims and {"claim", "source", "evidence_url"} <= set(claims[0])
    assert claims[0]["source"]["stage"] == "research"


def test_extract_claims_picks_outcome_sentences() -> None:
    texts = [c["claim"] for c in extract_claims(("research", SAMPLE))]
    assert any("improved merge latency" in t for t in texts)
    assert any("reduced prompt tokens" in t for t in texts)
    assert not any("neutral background prose" in t for t in texts)


def test_extract_claims_deterministic_for_identical_input() -> None:
    a = extract_claims(("r", SAMPLE), ("i", "# X\nFixed issue #7."))
    b = extract_claims(("r", SAMPLE), ("i", "# X\nFixed issue #7."))
    assert a == b


def test_extract_claims_dedupes_and_sorts() -> None:
    text = (
        "## A\nSwitching the index improved lookup speed by 30% overall.\n\n"
        "## B\nSwitching the index improved lookup speed by 30% overall."
    )
    claims = extract_claims(("research", text))
    assert [c["source"]["section"] for c in claims] == ["A", "B"]


def test_extract_claims_ignores_none_and_empty() -> None:
    assert extract_claims(("research", None), ("implementation", "")) == []


def test_triage_claim_verdicts_taxonomy() -> None:
    # 1. Verified via URL or metric
    c_url = triage_claim({
        "claim": "Improved speed significantly.",
        "evidence_url": "https://example.com/speed",
    })
    assert c_url["verdict"] == "verified"
    assert "justification" in c_url

    c_metric = triage_claim({
        "claim": "Fixed issue #42 and reduced latency by 35%.",
        "evidence_url": None,
    })
    assert c_metric["verdict"] == "verified"

    # 2. Falsified via contradiction/failure
    c_falsified = triage_claim({
        "claim": "The integration tests failed with 0 passed.",
        "evidence_url": "https://example.com",
    })
    assert c_falsified["verdict"] == "falsified"

    # 3. Toy-scale
    c_toy = triage_claim({
        "claim": "Demonstrated improved performance on a toy mock stub.",
        "evidence_url": None,
    })
    assert c_toy["verdict"] == "toy-scale"

    # 4. No-evidence
    c_no_ev = triage_claim({
        "claim": "This approach enables generalized improvements everywhere.",
        "evidence_url": None,
    })
    assert c_no_ev["verdict"] == "no-evidence"


def test_triage_claims_all_have_verdict_and_justification() -> None:
    raw_claims = extract_claims(("research", SAMPLE))
    triaged = triage_claims(raw_claims)
    assert len(triaged) == len(raw_claims)
    for c in triaged:
        assert c["verdict"] in CLAIM_VERDICTS
        assert len(c["justification"]) > 5


def test_compute_claim_score_calculation() -> None:
    claims = [
        {"claim": "A", "verdict": "verified"},  # 1.0
        {"claim": "B", "verdict": "toy-scale"},  # 0.3
        {"claim": "C", "verdict": "no-evidence"},  # 0.0
        {"claim": "D", "verdict": "falsified"},  # 0.0
    ]
    res = compute_claim_score(claims, dimension_max=50.0)
    # (1.0 + 0.3 + 0.0 + 0.0) / 4 = 1.3 / 4 = 32.5%
    assert res["overall_percentage"] == 32.5
    # 32.5% of 50.0 = 16.25 -> 16.2 or 16.3
    assert res["score"] == 16.2 or res["score"] == 16.3
    assert res["verdict_counts"] == {
        "verified": 1,
        "toy-scale": 1,
        "no-evidence": 1,
        "falsified": 1,
    }


def test_strict_grader_scores_from_claim_verdicts(tmp_path: Path) -> None:
    res_dir = tmp_path / "research"
    res_dir.mkdir(parents=True, exist_ok=True)
    res_file = res_dir / "2026-06-23.md"
    res_file.write_text(SAMPLE, encoding="utf-8")
    grader = StrictRubricJudgeGrader()
    scorecard = grader.score("2026-06-23", tmp_path)
    assert "claims" in scorecard
    assert "claim_verdicts" in scorecard
    assert scorecard["claim_verdicts"]["verified"] >= 1
    # Check that total_score was derived from claim verdicts, not self-assigned
    assert scorecard["overall_percentage"] > 0
    assert "rejection_bias" in scorecard
    assert "rejection_bias_flag" in scorecard


def test_detect_rejection_bias_patterns() -> None:
    text = """\
# Implementation Note
Although tests failed, this failure is expected and harmless for now.

# Architecture
Even though the script failed, the design is theoretically sound and conceptually valid.

# Root Cause
The timeout occurred due to the environment rather than our code.
"""
    detections = detect_rejection_bias([("implementation", text)])
    assert len(detections) >= 2
    categories = {d["category"] for d in detections}
    assert "dismissed_failure" in categories or "dismissed_failure_inline" in categories
    assert "theoretical_rationalization" in categories
    for d in detections:
        assert d["severity"] in ("high", "medium")
        assert len(d["reason"]) > 5


def test_detect_rejection_bias_clean_text() -> None:
    assert detect_rejection_bias([("research", SAMPLE)]) == []
    assert detect_rejection_bias([("research", None)]) == []


def test_strict_grader_rejection_bias_flag(tmp_path: Path) -> None:
    res_dir = tmp_path / "research"
    res_dir.mkdir(parents=True, exist_ok=True)
    res_file = res_dir / "2026-06-23.md"
    res_file.write_text(
        "# Finding\nAlthough tests failed, this failure is expected and harmless.\n",
        encoding="utf-8",
    )
    grader = StrictRubricJudgeGrader()
    scorecard = grader.score("2026-06-23", tmp_path)
    assert scorecard["rejection_bias_flag"] is True
    assert len(scorecard["rejection_bias"]) >= 1
    assert any("REJECTION_BIAS" in flag for flag in scorecard["flags"])


# ── Statistical-validity gate (#2696, P-Bench lesson) ────────────────


def test_stat_validity_grounded_claim_passes() -> None:
    # Correctly executed analysis: named test, p-value, sample size, source.
    claim = {
        "claim": (
            "Compared to the previous run, latency fell from 220ms to 140ms "
            "(t-test, p < 0.01, n=40). Source: https://example.com/bench"
        ),
        "evidence_url": "https://example.com/bench",
    }
    out = assess_stat_validity(claim)
    assert out["stat_validity"]["verdict"] == "ok"
    assert out["stat_validity"]["flags"] == []


def test_stat_validity_pvalue_without_method_is_suspect() -> None:
    # P-Bench failure mode: fluent statistic, no named method.
    out = assess_stat_validity({
        "claim": "The new retry policy significantly improved success (p = 0.031)."
    })
    verdict = out["stat_validity"]
    assert verdict["verdict"] == "suspect"
    assert "pvalue-without-method" in verdict["flags"]


def test_stat_validity_unbacked_precision_and_baseline_is_suspect() -> None:
    out = assess_stat_validity({
        "claim": "Adopting the parallel compactor improved merge latency by 52.7%."
    })
    verdict = out["stat_validity"]
    assert verdict["verdict"] == "suspect"
    assert "unbacked-precision" in verdict["flags"]
    assert "missing-baseline" in verdict["flags"]


def test_stat_validity_causal_from_correlation_is_suspect() -> None:
    out = assess_stat_validity({
        "claim": "Prefetching correlated with 12% lower latency, caused by better cache use."
    })
    verdict = out["stat_validity"]
    assert verdict["verdict"] == "suspect"
    assert "causal-from-correlation" in verdict["flags"]


def test_stat_validity_decisive_from_weak_is_suspect() -> None:
    # Weak qualifier precedes the decisive verb — order must not matter.
    out = assess_stat_validity({
        "claim": "Preliminary results prove a 40% cost reduction."
    })
    verdict = out["stat_validity"]
    assert verdict["verdict"] == "suspect"
    assert "decisive-from-weak" in verdict["flags"]


def test_stat_validity_non_quantitative_claim() -> None:
    out = assess_stat_validity({
        "claim": "This approach enables generalized improvements everywhere."
    })
    assert out["stat_validity"]["verdict"] == "non-quantitative"
    assert out["stat_validity"]["flags"] == []


def test_stat_validity_batch_attaches_dimension() -> None:
    raw = extract_claims((
        "research",
        "# Finding\nLatency dropped by 12% with no baseline.\n",
    ))
    triaged = assess_stat_validity_claims(triage_claims(raw))
    assert len(triaged) == len(raw)
    for c in triaged:
        assert "stat_validity" in c
        assert c["stat_validity"]["verdict"] in (
            "ok",
            "suspect",
            "non-quantitative",
        )


def test_stat_validity_stats_aggregate() -> None:
    claims = assess_stat_validity_claims([
        {"claim": "p = 0.031, no method named."},
        {"claim": "Cost fell from $10 to $8 (t-test, p < 0.05)."},
        {"claim": "This enables broad improvements."},
    ])
    stats = compute_stat_validity_stats(claims)
    assert stats["checked"] == 2
    assert stats["suspect"] == 1


def test_strict_grader_surfaces_stat_validity(tmp_path: Path) -> None:
    res_dir = tmp_path / "research"
    res_dir.mkdir(parents=True, exist_ok=True)
    (res_dir / "2026-06-23.md").write_text(
        "# Finding\nPreliminary results prove a 40% cost reduction.\n",
        encoding="utf-8",
    )
    scorecard = StrictRubricJudgeGrader().score("2026-06-23", tmp_path)
    assert scorecard["stat_validity"]["checked"] >= 1
    assert scorecard["stat_validity"]["suspect"] >= 1
    assert any("STAT_VALIDITY" in flag for flag in scorecard["flags"])
    for c in scorecard["claims"]:
        assert "stat_validity" in c
