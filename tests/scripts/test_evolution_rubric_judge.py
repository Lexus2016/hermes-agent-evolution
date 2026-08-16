"""Tests for claim-extraction in evolution_rubric_judge.py (#2482 Slice A / #2513).

Covers: extract_claims (markdown + JSON + determinism) and the ``claims`` key
now emitted by StrictRubricJudgeGrader.score().
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from evolution_rubric_judge import (  # noqa: E402
    StrictRubricJudgeGrader,
    extract_claims,
)


class TestExtractClaimsFromText:
    def test_bullets_and_headings_are_claims(self):
        md = "## Findings\n\n- Added a memory gate\n- Fixed stale analysis\n\n1. Merged two PRs\n"
        claims = extract_claims({"implementation": md})
        statements = [c["statement"] for c in claims]
        assert "Added a memory gate" in statements
        assert "Fixed stale analysis" in statements
        assert "Merged two PRs" in statements

    def test_source_and_location_are_recorded(self):
        md = "- Wired the gate into run_agent.py\n"
        claims = extract_claims({"implementation": md})
        assert claims == [
            {
                "statement": "Wired the gate into run_agent.py",
                "source": "implementation",
                "location": "line 1",
            }
        ]

    def test_duplicate_statements_deduplicated(self):
        md = "- Same claim\n- Same claim\n"
        claims = extract_claims({"implementation": md})
        assert len(claims) == 1

    def test_plain_paragraphs_are_not_claims(self):
        md = "This is a plain narrative paragraph.\n"
        assert extract_claims({"implementation": md}) == []


class TestExtractClaimsFromJson:
    def test_issue_titles_are_claims(self):
        data = {
            "issues": [
                {"title": "Add a dead-code gate", "priority_score": 1.78},
                {"title": "Fix the stale gate"},
            ]
        }
        claims = extract_claims({"issues": data})
        statements = {c["statement"] for c in claims}
        assert "Add a dead-code gate (priority 1.78)" in statements
        assert "Fix the stale gate" in statements

    def test_merged_count_is_a_claim(self):
        claims = extract_claims({"integration": {"merged_count": 3}})
        assert any("merged 3 PR(s)" in c["statement"] for c in claims)

    def test_none_input_yields_no_claims(self):
        assert extract_claims({"issues": None}) == []


class TestExtractClaimsDeterminism:
    def test_identical_input_yields_identical_output(self):
        outputs = {
            "research": "## Ideas\n\n- Proposal A\n- Proposal B\n",
            "issues": {"issues": [{"title": "Issue one"}]},
        }
        assert extract_claims(outputs) == extract_claims(outputs)


class TestScorecardIncludesClaims:
    def _write_stage(self, root: Path, subdir: str, date: str, name: str, text: str) -> None:
        d = root / subdir
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{date}.{name}").write_text(text, encoding="utf-8")

    def test_score_emits_claims_key(self, tmp_path):
        date = "2026-08-16"
        self._write_stage(
            tmp_path, "implementation", date, "md",
            "# Report\n\n- Implemented the gate\n- Closes #123\n",
        )
        self._write_stage(tmp_path, "research", date, "md", "## Findings\n\n- Proposal A\n")
        (tmp_path / "issues").mkdir(exist_ok=True)
        (tmp_path / "issues" / f"{date}.json").write_text(
            json.dumps({"issues": [{"title": "Issue one", "priority_score": 1.5}]}),
            encoding="utf-8",
        )

        scorecard = StrictRubricJudgeGrader().score(date, tmp_path)

        assert "claims" in scorecard
        statements = {c["statement"] for c in scorecard["claims"]}
        assert "Implemented the gate" in statements
        assert "Proposal A" in statements
        assert "Issue one (priority 1.5)" in statements
