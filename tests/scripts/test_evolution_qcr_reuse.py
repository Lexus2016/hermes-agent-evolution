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


def _capture(tmp_path, entries):  # one JSON line per capture record
    tmp_path.mkdir(parents=True, exist_ok=True)
    with open(tmp_path / "cap.jsonl", "a", encoding="utf-8") as fh:
        for e in entries:
            fh.write(json.dumps(e) + "\n")


def test_persistence_roundtrip_and_capture(tmp_path):
    # Only completed=True capture records become notes; notes roundtrip.
    _capture(tmp_path, [
        {"completed": True, "entries": [{"tool": "read_file"}, {"tool": "patch"},
                                        {"tool": "terminal"}]},
        {"completed": False, "entries": [{"tool": "web_search"}]},
    ])
    notes = notes_from_capture_dir(tmp_path)
    assert [n.source_task_type for n in notes] == ["coding"]
    assert "read_file" in notes[0].bindings_to_obtain

    store = tmp_path / "qcr-notes.jsonl"
    assert persist_notes(notes, store) == 1
    assert load_notes(store) == notes
    assert load_notes(tmp_path / "missing.jsonl") == []  # missing → empty
    store.write_text("not json\n\n{bad json}\n", encoding="utf-8")
    assert load_notes(store) == []  # malformed lines skipped, never raise


def test_guardrail_blocks_and_allows():
    ok = check_reuse_guardrail(
        build_qcr_note({"tools": ["read_file", "patch"]}, "coding"),
        target_task_type="coding", target_tools=["read_file", "patch"],
    )
    assert ok["ok"] is True and ok["must_re_resolve_before_replay"] is True

    mismatch = check_reuse_guardrail(
        build_qcr_note({"tools": ["web_search"]}, "research"),
        target_task_type="coding", target_tools=["web_search"],
    )
    assert mismatch["ok"] is False
    assert any("task-type mismatch" in r for r in mismatch["reasons"])

    no_env = check_reuse_guardrail(
        build_qcr_note({"tools": ["terminal", "patch"]}, "coding"),
        target_task_type="coding", target_tools=["patch"],
    )
    assert no_env["ok"] is False
    assert any("environment tools unavailable" in r for r in no_env["reasons"])

    partial = check_reuse_guardrail(
        build_qcr_note({"tools": ["read_file", "web_search"]}, "research"),
        target_task_type="research", target_tools=["web_search"],
    )
    assert partial["ok"] is True  # bindings re-resolve; they don't block
    assert "read_file" in partial["unresolved_bindings"]


def test_reuse_for_target_composed_path(tmp_path):
    store = tmp_path / "qcr-notes.jsonl"
    persist_notes([
        build_qcr_note({"tools": ["read_file", "patch"]}, "coding"),
        build_qcr_note({"tools": ["web_search"]}, "research"),
    ], store)
    ok = reuse_for_target(store, target_task_type="coding",
                          target_tools=["read_file", "patch"])
    assert ok["reusable"] is True and ok["note"]["source_task_type"] == "coding"

    miss = reuse_for_target(store, target_task_type="ops",
                            target_tools=["terminal"])
    assert miss["reusable"] is False and miss["reason"]

    empty = reuse_for_target(tmp_path / "none.jsonl", target_task_type="coding",
                             target_tools=["read_file"])
    assert empty["reusable"] is False and empty["best_score"] is None
