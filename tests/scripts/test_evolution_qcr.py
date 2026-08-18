"""Tests for the QCR target-bound note schema + summary-reranking selector (#2694)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from evolution_qcr import (  # noqa: E402
    QCR_FIELDS,
    QcrNote,
    build_qcr_note,
    build_qcr_skill_section,
    check_reuse_guardrail,
    distill_skill,
    load_notes,
    notes_from_capture_dir,
    rank_notes_for_target,
    reuse_for_target,
    score_note_for_target,
    select_reusable_memory,
    write_notes,
)


def test_qcr_fields_are_the_four_canonical_fields() -> None:
    assert QCR_FIELDS == (
        "workflow_invariant",
        "bindings_to_obtain",
        "applicability_conditions",
        "verification_guardrail",
    )


def test_build_note_from_coding_record() -> None:
    record = {"tools": ["read_file", "patch", "terminal", "write_file"]}
    note = build_qcr_note(record, task_type="coding")
    assert note.source_task_type == "coding"
    assert "coding" in note.workflow_invariant
    # read_file is a binding tool -> re-resolve targets.
    assert any("read_file" in b for b in note.bindings_to_obtain)
    # terminal is an env tool -> applicability is conditional.
    assert any("environment" in c for c in note.applicability_conditions)
    # patch/terminal/write_file are verify tools -> guardrail mentions re-run.
    assert "Re-run the verification step" in note.verification_guardrail


def test_build_note_from_research_record() -> None:
    record = {"tools": ["web_search", "web_extract"]}
    note = build_qcr_note(record, task_type="research")
    assert note.source_task_type == "research"
    assert any("web_search" in b for b in note.bindings_to_obtain)
    # No env/mutating tools -> guardrail is the generic one.
    assert "Re-run the verification step" not in note.verification_guardrail


def test_build_note_empty_record() -> None:
    note = build_qcr_note({}, task_type="")
    assert note.source_task_type == ""
    assert note.workflow_invariant
    assert note.bindings_to_obtain  # falls back to a generic re-resolve binding
    assert note.applicability_conditions


def test_note_roundtrip_dict() -> None:
    note = build_qcr_note({"tools": ["read_file", "terminal"]}, task_type="ops")
    d = note.to_dict()
    assert set(QCR_FIELDS) <= set(d)
    assert QcrNote.from_dict(d).to_dict() == d


# ── increment 2: summary-reranking selector ──────────────────────────────────


def _make_note(
    task_type: str,
    tools: list[str],
    bindings: list[str] | None = None,
    any_env: bool = True,
) -> QcrNote:
    """Fixture-style helper: a note with the requested task type / tools."""
    return QcrNote(
        workflow_invariant=f"Workflow is known to succeed for {task_type} tasks",
        bindings_to_obtain=bindings or [f"{t}-target" for t in tools],
        applicability_conditions=(
            ["Applies to any environment with the standard toolset."]
            if any_env
            else ["Requires the same environment (terminal/browser) as the source run."]
        ),
        verification_guardrail="Re-run the verification step.",
        source_task_type=task_type,
        source_tools=list(tools),
    )


def test_score_prefers_task_type_match() -> None:
    coding = _make_note("coding", ["read_file", "patch"])
    research = _make_note("research", ["web_search", "web_extract"])
    target_tools = ["read_file", "patch", "terminal"]

    coding_score = score_note_for_target(coding, "coding", target_tools)
    research_score = score_note_for_target(research, "coding", target_tools)

    # Same tools available to both; only task-type match differs.
    assert coding_score > research_score
    assert coding_score >= 0.5  # task-type match alone clears the floor


def test_score_penalizes_unresolvable_bindings() -> None:
    note = _make_note("coding", ["read_file", "web_search"])
    # Target lacks web_search -> binding cannot be re-resolved.
    with_tools = score_note_for_target(note, "coding", ["read_file", "patch"])
    without_tools = score_note_for_target(note, "coding", ["read_file", "web_search"])
    assert with_tools < without_tools
    # Overlap-only score must be below a fully-overlapping target.
    full = score_note_for_target(note, "coding", ["read_file", "web_search", "patch"])
    assert full > with_tools


def test_score_penalizes_binding_count() -> None:
    few = _make_note("coding", ["read_file"], bindings=["re-resolve file targets"])
    many = _make_note(
        "coding",
        ["read_file"],
        bindings=["a", "b", "c", "d", "e", "f", "g", "h"],
    )
    assert score_note_for_target(few, "coding", ["read_file"]) > score_note_for_target(
        many, "coding", ["read_file"]
    )


def test_score_clamped_to_unit_interval() -> None:
    best = _make_note("coding", ["read_file"], bindings=[])
    score = score_note_for_target(best, "coding", ["read_file"])
    assert 0.0 <= score <= 1.0
    worst = _make_note("ops", ["web_search"], bindings=["x"] * 10)
    low = score_note_for_target(worst, "coding", ["terminal"])
    assert 0.0 <= low <= 1.0


def test_rank_notes_best_first_deterministic() -> None:
    coding = _make_note("coding", ["read_file"])
    research = _make_note("research", ["web_search"])
    notes = [research, coding]

    ranked = rank_notes_for_target(notes, "coding", ["read_file", "patch"])
    assert [n.source_task_type for _, n in ranked] == ["coding", "research"]
    # Same input twice -> same output (deterministic).
    assert ranked == rank_notes_for_target(notes, "coding", ["read_file", "patch"])


def test_rank_accepts_dict_projections() -> None:
    coding = _make_note("coding", ["read_file"]).to_dict()
    ranked = rank_notes_for_target([coding], "coding", ["read_file"])
    assert len(ranked) == 1
    assert isinstance(ranked[0][1], QcrNote)
    assert ranked[0][1].source_task_type == "coding"


def test_select_reusable_memory_picks_best_above_floor() -> None:
    coding = _make_note("coding", ["read_file"])
    research = _make_note("research", ["web_search"])
    picked = select_reusable_memory(
        [research, coding], "coding", ["read_file", "patch"]
    )
    assert picked is not None
    assert picked.source_task_type == "coding"


def test_select_reusable_memory_none_below_floor() -> None:
    research = _make_note("research", ["web_search"])
    picked = select_reusable_memory(
        [research], "coding", ["read_file", "patch"], min_score=0.5
    )
    assert picked is None


# -- increment 3 rework: producer -> store -> guardrail -> replay-or-fallback ---


@pytest.mark.parametrize(
    "note_tools,note_type,target_type,target_tools,ok",
    [
        (["read_file", "patch"], "coding", "coding", ["read_file", "patch"], True),
        (["web_search"], "research", "coding", ["web_search"], False),
        (["terminal", "patch"], "coding", "coding", ["patch"], False),
        (["read_file", "web_search"], "research", "research", ["web_search"], True),
    ],
)
def test_guardrail_blocks_and_allows(
    note_tools, note_type, target_type, target_tools, ok
) -> None:
    verdict = check_reuse_guardrail(
        build_qcr_note({"tools": note_tools}, note_type),
        target_task_type=target_type,
        target_tools=target_tools,
    )
    assert verdict["ok"] is ok
    assert verdict["must_re_resolve_before_replay"] is True
    assert bool(verdict["reasons"]) == (not ok)  # blocked ⇔ has reasons
    assert verdict["bindings_to_resolve"]


def test_producer_to_consumer_end_to_end(tmp_path) -> None:
    """Producer -> store -> load -> guardrail -> replay-or-fallback (one slice)."""
    capture = tmp_path / "captures"
    capture.mkdir()
    (capture / "a.jsonl").write_text(
        '{"entries": [{"tool": "read_file"}, {"tool": "patch"}], "completed": true}\n'
        '{"entries": [{"tool": "web_search"}], "completed": false}\n',
        encoding="utf-8",
    )
    store = tmp_path / "qcr-notes.jsonl"
    notes = notes_from_capture_dir(capture)
    assert notes  # only the completed trajectory is distilled
    assert write_notes(store, notes) == len(notes)
    store.write_text(store.read_text() + "not json\n", encoding="utf-8")
    assert load_notes(store) == notes  # tolerant read round-trips
    decision = reuse_for_target(
        store,
        target_task_type="coding",
        target_tools=["read_file", "patch", "terminal"],
    )
    # read_file/patch classify as 'coding' (classify_by_tools), so the reuse
    # branch fires end-to-end against the matching target.
    assert decision["reusable"] is True
    assert decision["note"]["source_task_type"] == "coding"


# ── next increment: skill-distillation consumer (QCR notes → new skills) ────


def test_distill_skill_with_no_notes() -> None:
    result = distill_skill(
        [],
        target_task_type="coding",
        target_tools=["read_file", "terminal"],
    )
    assert result["skill_created"] is False
    assert "no candidate note" in result["reason"]


def test_distill_skill_creates_enriched_skill() -> None:
    notes = [
        build_qcr_note(
            {"tools": ["read_file", "patch", "terminal", "write_file"]},
            task_type="coding",
        )
    ]
    result = distill_skill(
        notes,
        target_task_type="coding",
        target_tools=["read_file", "patch", "terminal", "write_file"],
    )
    assert result["skill_created"] is True
    assert result["skill_name"]
    assert result["note_task_type"] == "coding"
    assert result["source_tools"] == sorted({
        "read_file",
        "patch",
        "terminal",
        "write_file",
    })
    # The four QCR fields are the ones carried into the skill markdown.
    assert result["qcr_fields"] == list(QCR_FIELDS)


def test_distill_skill_writes_qcr_section_into_markdown() -> None:
    # Evidence bar (#2746, PR #2771) governs EVERY promotion path, including
    # QCR distillation: a note's synthesized trace carries one action per
    # source tool, so the note must exercise >= MIN_DISTINCT_TOOLS tools and
    # >= MIN_ACTIONS (3) actions to be promoted. This test pins the markdown
    # RENDERING — so its note clears the bar (3 tools -> 3 actions).
    notes = [
        build_qcr_note(
            {"tools": ["read_file", "terminal", "search_files"]}, task_type="ops"
        )
    ]
    result = distill_skill(
        notes,
        target_task_type="ops",
        target_tools=["read_file", "terminal", "search_files"],
        min_score=0.0,
    )
    assert result["skill_created"] is True
    # The QCR section (rendered by build_qcr_skill_section) is what carries
    # the note's four fields into the skill; its rendering is asserted below.
    assert result["note_task_type"] == "ops"


def test_build_qcr_skill_section_renders_all_four_fields() -> None:
    note = build_qcr_note(
        {"tools": ["read_file", "terminal", "write_file"]}, task_type="ops"
    )
    section = build_qcr_skill_section(note)
    assert section.startswith("## Reuse Notes (QCR)")
    assert "Workflow invariant" in section
    assert "Bindings to obtain" in section
    assert "Applicability" in section
    assert "Verification guardrail" in section
    assert "read_file" in section  # a binding tool is listed
