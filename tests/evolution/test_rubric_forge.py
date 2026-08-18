# -*- coding: utf-8 -*-
"""RubricForge Slice 1 — primitive + real-consumer wiring tests (#2780)."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from evolution.lib.rubric_forge import score_rubric, select_best_rubric  # noqa: E402
from evolution_rubric_judge import (  # noqa: E402
    RUBRIC_DIMENSIONS,
    StrictRubricJudgeGrader,
    _keyword_requirements_judge,
    resolve_active_rubric,
)


def _judge(rubric: str, example: str) -> bool:
    return example in rubric


def test_score_rubric_agreement_math():
    agreement, per = score_rubric("alpha beta", ["alpha", "gamma"], [True, False], _judge)
    # alpha: verdict True == label True → match; gamma: False == False → match.
    assert agreement == 1.0 and per == [True, True]


def test_score_rubric_partial_and_judge_throw():
    def flaky(rubric: str, example: str) -> bool:
        if example == "boom":
            raise RuntimeError("nope")
        return True

    agreement, per = score_rubric("x", ["a", "boom"], [True, True], flaky)
    assert agreement == 0.5 and per == [True, False]


def test_select_best_rubric_first_candidate_wins_ties():
    best, agr = select_best_rubric(
        ["alpha", "alpha beta", "alpha"], ["alpha"], [True], _judge
    )
    assert best == "alpha" and agr == 1.0  # strict > keeps the FIRST at 1.0
    assert select_best_rubric([], ["a"], [True], _judge) == ("", 0.0)


def _seed_rf(evolution_dir: Path, candidates, labeled):
    rf = evolution_dir / "rubric-forge"
    rf.mkdir(parents=True, exist_ok=True)
    (rf / "candidates.json").write_text(json.dumps(candidates), encoding="utf-8")
    (rf / "labeled.json").write_text(json.dumps(labeled), encoding="utf-8")


def test_resolve_active_rubric_selects_and_audits(tmp_path):
    _seed_rf(
        tmp_path,
        candidates=[
            "research: 12\nmentions provenance and citations",
            "research: 4\nmentions provenance, citations, reproduction steps",
        ],
        labeled=[
            {"requires": ["provenance"], "label": True},
            {"requires": ["citations", "reproduction steps"], "label": True},
            {"forbids": ["vibes"], "label": False},
        ],
    )
    active = resolve_active_rubric(tmp_path)
    assert active is not None
    # The second candidate satisfies all three labeled examples (agreement 1.0).
    assert "reproduction steps" in active["rubric"]
    assert active["agreement"] == 1.0
    assert active["max_overrides"] == {"research": 4.0}
    audit = json.loads((tmp_path / "rubric-forge" / "selected.json").read_text())
    assert audit["agreement"] == 1.0 and audit["max_overrides"] == {"research": 4.0}


def test_resolve_active_rubric_absent_inputs_is_none(tmp_path):
    assert resolve_active_rubric(tmp_path) is None
    _seed_rf(tmp_path, candidates=[], labeled=[])
    assert resolve_active_rubric(tmp_path) is None


def test_score_applies_override_max(tmp_path, monkeypatch):
    """The judge's score() CONSUMES the winning rubric: dimension max follows
    the override line, and the run carries the rubric_forge verdict."""
    _seed_rf(
        tmp_path,
        candidates=["research: 3\nmentions sources"],
        labeled=[{"requires": ["sources"], "label": True}],
    )
    judge = StrictRubricJudgeGrader()
    result = judge.score("2026-08-18", evolution_dir=tmp_path)
    assert result["dimensions"]["research"]["max"] == 3.0
    assert result["rubric_forge"]["max_overrides"] == {"research": 3.0}
    # Untouched dimensions keep the shipped maxes.
    assert result["dimensions"]["issues"]["max"] == float(
        RUBRIC_DIMENSIONS["issues"]["max"]
    )


def test_keyword_judge_require_and_forbid_semantics():
    assert _keyword_requirements_judge(
        "must cite provenance", {"requires": ["provenance"], "label": True}
    )
    assert not _keyword_requirements_judge(
        "must cite provenance", {"requires": ["urls"], "label": True}
    )
    # label=False example with a present forbidden term: judge True != label
    # False → the rubric DISAGREES (loses agreement).
    assert _keyword_requirements_judge("talks about vibes", {"forbids": ["vibes"]})
    assert not _keyword_requirements_judge("rigorous", {"forbids": ["vibes"]})
