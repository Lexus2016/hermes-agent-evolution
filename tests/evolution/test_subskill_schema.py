# -*- coding: utf-8 -*-
"""Unit tests for the sub-skill schema (issue #3070).

Covers serialization round-trips, validation, provenance tagging, the
human-review approval gate, and compatibility with the skill-hub bundle
metadata shape used by ``tools.skills_hub``.
"""

from __future__ import annotations

import pytest

from evolution.lib.subskill_schema import (
    CapabilityFragment,
    Precondition,
    SubSkill,
    SubSkillProvenance,
    SubSkillStatus,
    subskill_to_bundle_meta,
    validate_subskill,
)


def _sample_subskill(**overrides) -> SubSkill:
    base = SubSkill(
        name="git-commit-and-push",
        description="Commit staged changes and push to the current branch.",
        precondition=Precondition(
            intent_keywords=["commit", "push"],
            required_tool_names=["terminal"],
            required_state_keys=["git_repo"],
        ),
        capability=CapabilityFragment(
            description="Run git add/commit/push in sequence.",
            tool_sequence=["terminal", "terminal", "terminal"],
            parameter_schema={"commit_message": {"type": "string"}},
        ),
        provenance=SubSkillProvenance(
            source_task_id="task-42",
            source_session_id="sess-7",
        ),
        tags=["git", "vcs"],
    )
    for k, v in overrides.items():
        setattr(base, k, v)
    return base


# --- Serialization ---------------------------------------------------------


def test_round_trip_preserves_all_fields():
    skill = _sample_subskill()
    restored = SubSkill.from_dict(skill.to_dict())
    assert restored.to_dict() == skill.to_dict()
    assert restored.name == "git-commit-and-push"
    assert restored.precondition.intent_keywords == ["commit", "push"]
    assert restored.capability.tool_sequence == ["terminal", "terminal", "terminal"]
    assert restored.provenance.source_task_id == "task-42"


def test_from_dict_ignores_unknown_fields():
    data = _sample_subskill().to_dict()
    data["bogus_field"] = "ignored"
    restored = SubSkill.from_dict(data)
    assert not hasattr(restored, "bogus_field")
    assert restored.name == "git-commit-and-push"


def test_id_slug_is_normalized():
    skill = SubSkill(name="My Sub Skill: v2!", description="d")
    assert skill.id_slug == "my-sub-skill-v2"


# --- Validation ------------------------------------------------------------


def test_valid_subskill_passes_validation():
    report = validate_subskill(_sample_subskill())
    assert report["valid"] is True
    assert report["errors"] == []


def test_missing_name_fails_validation():
    report = validate_subskill(_sample_subskill(name=""))
    assert report["valid"] is False
    assert "name is empty" in report["errors"]


def test_empty_precondition_fails_validation():
    skill = _sample_subskill()
    skill.precondition = Precondition()
    report = validate_subskill(skill)
    assert report["valid"] is False
    assert any("precondition" in e for e in report["errors"])


def test_empty_capability_sequence_fails_validation():
    skill = _sample_subskill()
    skill.capability = CapabilityFragment(description="no tools")
    report = validate_subskill(skill)
    assert report["valid"] is False
    assert any("tool_sequence" in e for e in report["errors"])


def test_unknown_status_fails_validation():
    skill = _sample_subskill(status="bogus")
    report = validate_subskill(skill)
    assert report["valid"] is False
    assert any("status" in e for e in report["errors"])


# --- Provenance tagging ----------------------------------------------------


def test_provenance_records_source_and_timestamp():
    skill = _sample_subskill()
    assert skill.provenance.source_task_id == "task-42"
    assert skill.provenance.source_session_id == "sess-7"
    assert skill.provenance.extracted_at  # non-empty ISO timestamp


def test_approve_tags_human_reviewer_and_flips_status():
    skill = _sample_subskill()
    assert skill.status == SubSkillStatus.EXTRACTED
    assert skill.review_required is True
    skill.approve("alice")
    assert skill.status == SubSkillStatus.APPROVED
    assert skill.review_required is False
    assert skill.provenance.human_reviewer == "alice"
    assert skill.provenance.reviewed_at


def test_reject_tags_human_reviewer_and_flips_status():
    skill = _sample_subskill()
    skill.reject("bob")
    assert skill.status == SubSkillStatus.REJECTED
    assert skill.review_required is False
    assert skill.provenance.human_reviewer == "bob"


# --- Precondition match scoring --------------------------------------------


def test_match_score_returns_one_for_full_overlap():
    skill = _sample_subskill()
    score = skill.precondition.match_score(
        "please commit and push", ["terminal"], ["git_repo"]
    )
    assert score == pytest.approx(1.0)


def test_match_score_returns_zero_for_no_overlap():
    skill = _sample_subskill()
    score = skill.precondition.match_score("deploy to prod", ["web"], ["cluster"])
    assert score == pytest.approx(0.0)


def test_match_score_is_partial_for_partial_overlap():
    skill = _sample_subskill()
    score = skill.precondition.match_score("commit", ["terminal"], ["git_repo"])
    # intent keywords: 1/2 hit; tools: 1/1; state: 1/1 -> mean of 0.5, 1.0, 1.0
    assert score == pytest.approx((0.5 + 1.0 + 1.0) / 3.0)


# --- Skill-hub bundle meta compatibility -----------------------------------


def test_bundle_meta_round_trips_through_skill_hub_shape():
    skill = _sample_subskill()
    meta = subskill_to_bundle_meta(skill)
    # Shape expected by tools.skills_hub.SkillBundle / SkillMeta.
    assert meta["name"] == "git-commit-and-push"
    assert meta["source"] == "subskill"
    assert meta["trust_level"] == "agent"
    assert meta["identifier"].startswith("subskill/")
    assert meta["extra"]["subskill_id"] == skill.subskill_id
    assert meta["extra"]["status"] == SubSkillStatus.EXTRACTED
    assert meta["extra"]["precondition"]["intent_keywords"] == ["commit", "push"]
    assert meta["extra"]["capability"]["tool_sequence"] == [
        "terminal",
        "terminal",
        "terminal",
    ]
    assert meta["extra"]["provenance"]["source_task_id"] == "task-42"


def test_bundle_meta_is_json_serializable():
    import json

    meta = subskill_to_bundle_meta(_sample_subskill())
    # Must survive a JSON round-trip (audit log / hub persistence).
    dumped = json.dumps(meta)
    assert json.loads(dumped) == meta
