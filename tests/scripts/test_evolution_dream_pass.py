"""Tests for the grade-weighted dream pass (#1875) and the tqmemory-backed
contradiction scanner (issue #48 slice 1, rework of PR #3110)."""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone, UTC
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

from evolution_dream_pass import (
    MAX_OLDER_AGE_DAYS,
    SUPERSEDED_NOTE_STATUS,
    apply_tqmemory_deprecations,
    classify_cycle,
    dream_pass,
    dream_contradiction_scan,
    load_notes,
    load_records,
    load_tqmemory_notes,
    scan_tqmemory_contradictions,
)


def _jl(path: Path, rows: list[dict]) -> None:
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")


def _cy(d: str, m: int, s: int, r: int) -> dict:
    return {"date": d, "merged": m, "selected": s, "rejected": r}


def _note(i: str, c: str, w: float = 1.0) -> dict:
    return {"id": i, "cycle": c, "weight": w, "tags": []}


def test_classify() -> None:
    assert classify_cycle(_cy("d", 2, 3, 1)) == "high-grade"
    assert classify_cycle(_cy("d", 0, 1, 2)) == "revision-needed"
    assert classify_cycle(_cy("d", 0, 0, 0)) == "neutral"
    assert classify_cycle(_cy("d", 1, 4, 0)) == "neutral"


def test_dream_pass_promotes_and_tags(tmp_path: Path) -> None:
    metrics, notes = tmp_path / "metrics.jsonl", tmp_path / "notes.jsonl"
    _jl(
        metrics,
        [
            _cy("2026-08-01", 2, 3, 1),
            _cy("2026-08-02", 0, 1, 2),
            _cy("2026-08-03", 0, 0, 0),
        ],
    )
    _jl(
        notes,
        [
            _note("n1", "2026-08-01"),
            _note("n2", "2026-08-02"),
            _note("n3", "2026-08-03"),
        ],
    )
    s = dream_pass(metrics, notes)
    assert s["cycles_reviewed"] == 3
    assert s["high_grade"] == ["2026-08-01"] and s["revision_needed"] == ["2026-08-02"]
    assert s["neutral"] == ["2026-08-03"]
    assert s["notes_promoted"] == 1 and s["notes_tagged"] == 1
    up = {n["id"]: n for n in load_notes(notes)}
    assert up["n1"]["weight"] == 1.5 and "promoted" in up["n1"]["tags"]
    assert "failure:unmerged" in up["n2"]["tags"]
    assert up["n3"]["tags"] == [] and up["n3"]["weight"] == 1.0
    assert (tmp_path / "dream_pass.json").exists()


def test_empty_inputs(tmp_path: Path) -> None:
    s = dream_pass(tmp_path / "metrics.jsonl", tmp_path / "notes.jsonl")
    assert s["cycles_reviewed"] == 0 and s["notes_promoted"] == 0
    assert s["notes_tagged"] == 0 and (tmp_path / "dream_pass.json").exists()


def test_weight_cap_and_recent_limit(tmp_path: Path) -> None:
    metrics, notes = tmp_path / "metrics.jsonl", tmp_path / "notes.jsonl"
    _jl(metrics, [_cy(f"2026-08-{i:02d}", 2, 3, 1) for i in range(1, 11)])
    _jl(notes, [_note(f"n{i}", f"2026-08-{i:02d}", 1.8) for i in range(1, 11)])
    s = dream_pass(metrics, notes, recent=3)
    assert s["cycles_reviewed"] == 3
    up = {n["id"]: n for n in load_notes(notes)}
    assert up["n8"]["weight"] == 2.0  # capped
    assert up["n1"]["weight"] == 1.8  # outside recent window, untouched


def test_load_records_skips_malformed(tmp_path: Path) -> None:
    f = tmp_path / "metrics.jsonl"
    f.write_text(
        '{"date":"x","merged":1}\nnot-json\n\n{"date":"y"}\n', encoding="utf-8"
    )
    assert [r["date"] for r in load_records(f)] == ["x", "y"]


# --- Contradiction scanner over the tqmemory store (issue #48, slice 1) -----

TS0 = "2026-08-01T00:00:00Z"
TS1 = "2026-08-02T00:00:00Z"


def _tq(
    note_id: str,
    title: str,
    created_at: str,
    status: str = "active",
    project_id: str = "proj-a",
    **extra: object,
) -> dict:
    note: dict = {
        "content": "body",
        "created_at": created_at,
        "note_id": note_id,
        "note_kind": "decision",
        "note_status": status,
        "project_id": project_id,
        "project_name": "proj-a",
        "scope": "project",
        "source_refs": ["session://x"],
        "tags": ["t"],
        "title": title,
        "updated_at": created_at,
    }
    note.update(extra)
    return note


def _write_store(tmp_path: Path, notes: list[dict]) -> list[dict]:
    """Write notes into a fake tqmemory layout and return them with source_path."""
    out: list[dict] = []
    for n in notes:
        d = tmp_path / "projects" / n["project_id"] / "notes"
        d.mkdir(parents=True, exist_ok=True)
        f = d / f"{n['note_id']}.json"
        f.write_text(json.dumps(n, indent=2) + "\n", encoding="utf-8")
        n = dict(n)
        n["source_path"] = str(f)
        out.append(n)
    return out


def test_scan_detects_newer_overrides_older() -> None:
    notes = [
        _tq("n1", "Tool X failure", TS0),
        _tq("n2", "Tool X failure", TS1),
    ]
    props = scan_tqmemory_contradictions(notes)
    assert len(props) == 1
    p = props[0]
    assert p["older_id"] == "n1" and p["newer_id"] == "n2"
    assert p["topic"] == "tool x failure"
    assert p["resolution"] == "newer-overrides-older"
    assert p["action"] == "deprecate-older"


def test_scan_reversed_timestamps_flip_roles() -> None:
    notes = [
        _tq("n1", "Tool X failure", TS1),  # newer timestamp, listed first
        _tq("n2", "Tool X failure", TS0),  # older timestamp
    ]
    props = scan_tqmemory_contradictions(notes)
    assert len(props) == 1
    assert props[0]["older_id"] == "n2" and props[0]["newer_id"] == "n1"


def test_scan_three_active_newest_wins_for_all() -> None:
    notes = [
        _tq("n1", "Tool X failure", TS0),
        _tq("n2", "Tool X failure", TS1),
        _tq("n3", "Tool X failure", "2026-08-03T00:00:00Z"),
    ]
    props = scan_tqmemory_contradictions(notes)
    assert len(props) == 2
    assert {p["older_id"] for p in props} == {"n1", "n2"}
    assert all(p["newer_id"] == "n3" for p in props)


def test_scan_superseded_older_is_inert() -> None:
    notes = [
        _tq("n1", "Tool X failure", TS0, status=SUPERSEDED_NOTE_STATUS),
        _tq("n2", "Tool X failure", TS1),
    ]
    assert scan_tqmemory_contradictions(notes) == []


def test_scan_identical_timestamps_skipped() -> None:
    notes = [
        _tq("n1", "Tool X failure", TS0),
        _tq("n2", "Tool X failure", TS0),
    ]
    assert scan_tqmemory_contradictions(notes) == []


def test_scan_missing_created_at_skipped() -> None:
    notes = [
        _tq("n1", "Tool X failure", TS0),
        _tq("n2", "Tool X failure", "not-a-date"),
    ]
    assert scan_tqmemory_contradictions(notes) == []


def test_scan_untitled_notes_skipped() -> None:
    notes = [
        _tq("n1", "", TS0),
        _tq("n2", "Tool X failure", TS0),
        _tq("n3", "Tool X failure", TS1),
    ]
    props = scan_tqmemory_contradictions(notes)
    assert len(props) == 1
    assert props[0]["older_id"] == "n2" and props[0]["newer_id"] == "n3"


def test_scan_age_window_respects_max_older_age() -> None:
    now = datetime(2026, 8, 23, tzinfo=UTC)
    old = (now - timedelta(days=MAX_OLDER_AGE_DAYS + 1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    fresh = (now - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    notes = [
        _tq("n1", "Tool X failure", old),
        _tq("n2", "Tool X failure", fresh),
    ]
    assert scan_tqmemory_contradictions(notes, now=now) == []
    notes[0]["created_at"] = (now - timedelta(days=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
    props = scan_tqmemory_contradictions(notes, now=now)
    assert len(props) == 1 and props[0]["older_id"] == "n1"


def test_apply_writes_superseded_record_and_is_idempotent(tmp_path: Path) -> None:
    notes = _write_store(
        tmp_path,
        [
            _tq("n1", "Tool X failure", TS0),
            _tq("n2", "Tool X failure", TS1),
        ],
    )
    props = scan_tqmemory_contradictions(notes)
    assert apply_tqmemory_deprecations(notes, props) == 1

    on_disk = json.loads(Path(notes[0]["source_path"]).read_text(encoding="utf-8"))
    assert on_disk["note_status"] == SUPERSEDED_NOTE_STATUS
    assert on_disk["deprecated_at"] and on_disk["updated_at"]
    assert "superseded" in on_disk["deprecation_reason"]
    assert on_disk["superseded_by"]["note_id"] == "n2"
    assert on_disk["superseded_by"]["source_path"] == notes[1]["source_path"]
    # The newer note is untouched.
    newer = json.loads(Path(notes[1]["source_path"]).read_text(encoding="utf-8"))
    assert newer["note_status"] == "active" and "deprecated_at" not in newer

    # Idempotent: a second apply over the same proposals does nothing.
    assert apply_tqmemory_deprecations(notes, props) == 0


def test_apply_dry_run_writes_nothing(tmp_path: Path) -> None:
    notes = _write_store(
        tmp_path,
        [
            _tq("n1", "Tool X failure", TS0),
            _tq("n2", "Tool X failure", TS1),
        ],
    )
    props = scan_tqmemory_contradictions(notes)
    assert apply_tqmemory_deprecations(notes, props, dry_run=True) == 1
    on_disk = json.loads(Path(notes[0]["source_path"]).read_text(encoding="utf-8"))
    assert on_disk["note_status"] == "active"


def test_loader_reads_store_layout_and_skips_malformed(tmp_path: Path) -> None:
    _notes = _write_store(
        tmp_path,
        [
            _tq("n1", "One", TS0, project_id="proj-a"),
            _tq("n2", "Two", TS0, project_id="proj-b"),
        ],
    )
    (tmp_path / "projects" / "proj-a" / "notes" / "broken.json").write_text(
        "{not-json", encoding="utf-8"
    )
    loaded = load_tqmemory_notes(tmp_path)
    assert {n["note_id"] for n in loaded} == {"n1", "n2"}
    assert all(n["source_path"] for n in loaded)
    # Missing root -> empty, not a crash.
    assert load_tqmemory_notes(tmp_path / "does-not-exist") == []


def test_dream_contradiction_scan_end_to_end(tmp_path: Path) -> None:
    _write_store(
        tmp_path,
        [
            _tq("n1", "Tool X failure", TS0),
            _tq("n2", "Tool X failure", TS1),
            _tq("n3", "Lone note", TS1),
        ],
    )
    summary = dream_contradiction_scan(tmp_path)
    assert summary["notes_scanned"] == 3
    assert summary["active_notes"] == 3
    assert summary["contradictions_found"] == 1
    assert summary["deprecations_applied"] == 1
    assert summary["dry_run"] is False
    assert summary["proposals"][0]["older_id"] == "n1"

    # Second run: nothing left to do.
    again = dream_contradiction_scan(tmp_path)
    assert again["contradictions_found"] == 0
    assert again["deprecations_applied"] == 0


def test_dream_contradiction_scan_dry_run(tmp_path: Path) -> None:
    _write_store(
        tmp_path,
        [
            _tq("n1", "Tool X failure", TS0),
            _tq("n2", "Tool X failure", TS1),
        ],
    )
    summary = dream_contradiction_scan(tmp_path, dry_run=True)
    assert summary["contradictions_found"] == 1
    assert summary["deprecations_applied"] == 1
    assert summary["dry_run"] is True
    # Nothing written to disk.
    assert (
        json.loads(
            Path(tmp_path / "projects" / "proj-a" / "notes" / "n1.json").read_text(
                encoding="utf-8"
            )
        )["note_status"]
        == "active"
    )
