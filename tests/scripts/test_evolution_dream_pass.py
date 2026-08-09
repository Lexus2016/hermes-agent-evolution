"""Tests for the grade-weighted dream pass (#1875)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

from evolution_dream_pass import classify_cycle, dream_pass, load_notes, load_records  # noqa: E402


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
