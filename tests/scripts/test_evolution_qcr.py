"""Tests for the QCR target-bound note schema (#2694)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from evolution_qcr import (  # noqa: E402
    QCR_FIELDS,
    QcrNote,
    build_qcr_note,
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
