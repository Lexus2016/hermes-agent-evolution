"""Tests for QCR increment 3: note persistence + replay guardrail (#2694)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from evolution_qcr import (  # noqa: E402
    build_qcr_note,
    check_reuse_guardrail,
    load_notes,
    notes_from_capture_dir,
    persist_notes,
    reuse_for_target,
)


def _write_capture_entry(directory: Path, tools, completed=True, name="cap.jsonl"):
    d = Path(directory)
    d.mkdir(parents=True, exist_ok=True)
    entry = {
        "completed": completed,
        "entries": [{"tool": t} for t in tools],
    }
    with open(d / name, "a", encoding="utf-8") as fh:  # append: one record/line
        fh.write(json.dumps(entry) + "\n")


# -- persistence: trajectories BECOME notes ---------------------------------


def test_notes_from_capture_dir_distills_successful_runs(tmp_path):
    _write_capture_entry(tmp_path, ["read_file", "patch", "terminal"])
    _write_capture_entry(tmp_path, ["web_search"], completed=False)  # skipped
    notes = notes_from_capture_dir(tmp_path)
    assert len(notes) == 1  # only the successful run becomes a note
    assert notes[0].source_task_type == "coding"  # classify_by_tools
    assert "read_file" in notes[0].bindings_to_obtain


def test_persist_and_load_notes_roundtrip(tmp_path):
    notes = [build_qcr_note({"tools": ["read_file"]}, "coding")]
    store = tmp_path / "qcr-notes.jsonl"
    assert persist_notes(notes, store) == 1
    loaded = load_notes(store)
    assert loaded == notes


def test_load_notes_tolerates_missing_and_malformed(tmp_path):
    assert load_notes(tmp_path / "missing.jsonl") == []
    bad = tmp_path / "bad.jsonl"
    bad.write_text("not json\n\n{malformed}\n", encoding="utf-8")
    assert load_notes(bad) == []


# -- replay guardrail --------------------------------------------------------


def test_guardrail_ok_on_matching_target():
    note = build_qcr_note({"tools": ["read_file", "patch"]}, "coding")
    v = check_reuse_guardrail(
        note, target_task_type="coding", target_tools=["read_file", "patch"]
    )
    assert v["ok"] is True and v["reasons"] == []
    assert v["must_re_resolve_before_replay"] is True


def test_guardrail_blocks_task_type_mismatch():
    note = build_qcr_note({"tools": ["web_search"]}, "research")
    v = check_reuse_guardrail(
        note, target_task_type="coding", target_tools=["web_search"]
    )
    assert v["ok"] is False
    assert any("task-type mismatch" in r for r in v["reasons"])


def test_guardrail_blocks_missing_env_tools():
    note = build_qcr_note({"tools": ["terminal", "patch"]}, "coding")
    v = check_reuse_guardrail(note, target_task_type="coding", target_tools=["patch"])
    assert v["ok"] is False
    assert any("environment tools unavailable" in r for r in v["reasons"])


def test_guardrail_flags_unresolved_binding_tools():
    note = build_qcr_note({"tools": ["read_file", "web_search"]}, "research")
    v = check_reuse_guardrail(
        note, target_task_type="research", target_tools=["web_search"]
    )
    assert v["ok"] is True  # information only; replay proceeds, bindings re-resolved
    assert "read_file" in v["unresolved_bindings"]
    assert "read_file" in v["bindings_to_resolve"]


# -- the composed replay path ------------------------------------------------


def test_reuse_for_target_end_to_end(tmp_path):
    store = tmp_path / "qcr-notes.jsonl"
    persist_notes(
        [
            build_qcr_note({"tools": ["read_file", "patch"]}, "coding"),
            build_qcr_note({"tools": ["web_search"]}, "research"),
        ],
        store,
    )
    ok = reuse_for_target(
        store, target_task_type="coding", target_tools=["read_file", "patch"]
    )
    assert ok["reusable"] is True
    assert ok["note"]["source_task_type"] == "coding"

    mismatch = reuse_for_target(
        store, target_task_type="ops", target_tools=["terminal"]
    )
    assert mismatch["reusable"] is False
    assert mismatch["reason"]


def test_reuse_for_target_empty_store(tmp_path):
    out = reuse_for_target(tmp_path / "none.jsonl", target_task_type="coding",
                           target_tools=["read_file"])
    assert out["reusable"] is False and out["best_score"] is None


def test_note_dict_projection_guardrail():
    note = build_qcr_note({"tools": ["read_file"]}, "coding")
    v = check_reuse_guardrail(
        note.to_dict(), target_task_type="coding", target_tools=["read_file"]
    )
    assert v["ok"] is True
