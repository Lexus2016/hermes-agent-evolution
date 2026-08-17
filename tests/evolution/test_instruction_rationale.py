"""Tests for instruction-rationale provenance (#2629, arXiv:2608.11095)."""

from __future__ import annotations

from pathlib import Path

from evolution.lib.instruction_rationale import (
    check_instruction_file,
    extract_frontmatter,
    rationale_referenced,
    scan_skills_dir,
    validate_rationale,
)

GOOD = """---
name: evolution-analysis
rationale:
  failure: selection was stale at implementation time
  hypothesis: a freshness gate on the analysis artifact prevents stale picks
  outcome: confirmed - stale inputs now gate downstream stages
---

# Skill

Body mentions the selection freshness gate stays here.
"""

NO_RATIONALE = """---
name: evolution-analysis
---

# Skill

No rationale block at all.
"""
DECAYED = """---
name: evolution-analysis
rationale:
  failure: selection was stale at implementation time
  hypothesis: freshness gate
  outcome: confirmed
---

# Skill

The body no longer references the failure phrase anywhere.
"""


def test_extract_frontmatter_and_validate_good():
    fm = extract_frontmatter(GOOD)
    assert fm is not None and fm["name"] == "evolution-analysis"
    assert validate_rationale(fm) == []


def test_missing_or_malformed_rationale_flagged():
    fm = extract_frontmatter(NO_RATIONALE)
    assert "missing 'rationale' block" in validate_rationale(fm)[0]
    assert "missing YAML frontmatter" in validate_rationale(None)
    assert any(
        "failure" in p for p in validate_rationale({"rationale": {"outcome": "x"}})
    )


def test_rationale_referenced_and_decay_detection():
    assert rationale_referenced(GOOD, extract_frontmatter(GOOD)) is True
    assert rationale_referenced(DECAYED, extract_frontmatter(DECAYED)) is False


def test_check_instruction_file_and_scan(tmp_path: Path):
    good = tmp_path / "good" / "SKILL.md"
    good.parent.mkdir()
    good.write_text(GOOD, encoding="utf-8")
    bad = tmp_path / "bad" / "SKILL.md"
    bad.parent.mkdir()
    bad.write_text(NO_RATIONALE, encoding="utf-8")

    assert check_instruction_file(good).ok is True
    assert check_instruction_file(bad).ok is False

    reports = scan_skills_dir(tmp_path)
    assert len(reports) == 2
    assert {r.ok for r in reports} == {True, False}

    report = check_instruction_file(tmp_path / "nope" / "SKILL.md")
    assert report.ok is False and report.problems
