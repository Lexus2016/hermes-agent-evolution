"""Tests for the grade-weighted dream pass (issue #1875)."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "scripts"))
from evolution_dream_pass import (  # noqa: E402
    classify_cycle,
    find_cycle_notes,
    load_recent_metrics,
    run_dream_pass,
)


def test_classify_high_grade():
    assert (
        classify_cycle({"merged": 2, "skipped": 0, "rejected": 1, "selected": 2})
        == "high-grade"
    )


def test_classify_revision_needed_skips():
    assert (
        classify_cycle({"merged": 1, "skipped": 2, "rejected": 0, "selected": 3})
        == "revision-needed"
    )


def test_classify_revision_needed_no_merge():
    assert (
        classify_cycle({"merged": 0, "skipped": 0, "rejected": 3, "selected": 2})
        == "revision-needed"
    )


def test_classify_neutral():
    assert (
        classify_cycle({"merged": 0, "skipped": 0, "rejected": 0, "selected": 0})
        == "neutral"
    )


def test_classify_high_grade_threshold():
    assert (
        classify_cycle({"merged": 1, "skipped": 0, "rejected": 3, "selected": 1})
        != "high-grade"
    )


def test_load_metrics_empty(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    assert load_recent_metrics() == []


def test_load_metrics(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    evo = tmp_path / "evolution"
    evo.mkdir()
    (evo / "metrics.jsonl").write_text(
        json.dumps({"date": "2026-01-01", "merged": 1})
        + "\n"
        + json.dumps({"date": "2026-01-02", "merged": 0})
        + "\n"
    )
    results = load_recent_metrics()
    assert len(results) == 2
    assert results[0]["date"] == "2026-01-01"


def test_load_metrics_max(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    evo = tmp_path / "evolution"
    evo.mkdir()
    lines = "\n".join(
        json.dumps({"date": f"2026-01-{i:02d}", "merged": i}) for i in range(1, 6)
    )
    (evo / "metrics.jsonl").write_text(lines + "\n")
    results = load_recent_metrics(max_cycles=2)
    assert len(results) == 2
    assert results[-1]["date"] == "2026-01-05"


def test_find_notes_empty(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    assert find_cycle_notes("2026-01-01") == []


def test_find_notes(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    notes = tmp_path / "tqmemory" / "notes"
    notes.mkdir(parents=True)
    (notes / "n1.json").write_text(
        json.dumps({
            "id": "note-1",
            "source_refs": ["evolution-cycle-2026-01-01", "file://foo.py"],
        })
    )
    (notes / "n2.json").write_text(
        json.dumps({"id": "note-2", "source_refs": ["evolution-cycle-2026-02-01"]})
    )
    assert find_cycle_notes("2026-01-01") == ["note-1"]


def test_run_dream_pass_noop(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    result = run_dream_pass()
    assert result["status"] == "noop"
    assert result["adjusted"] == 0


def test_run_dream_pass_high_grade(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    evo = tmp_path / "evolution"
    evo.mkdir()
    (evo / "metrics.jsonl").write_text(
        json.dumps({
            "date": "2026-01-01",
            "merged": 2,
            "skipped": 0,
            "rejected": 1,
            "selected": 2,
        })
        + "\n"
    )
    result = run_dream_pass()
    assert result["status"] == "ok"
    assert result["high_grade"] == 1
    assert (evo / "dream-pass-result.json").exists()


def test_run_dream_pass_revision_needed(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    evo = tmp_path / "evolution"
    evo.mkdir()
    (evo / "metrics.jsonl").write_text(
        json.dumps({
            "date": "2026-01-01",
            "merged": 0,
            "skipped": 2,
            "rejected": 0,
            "selected": 3,
        })
        + "\n"
    )
    result = run_dream_pass()
    assert result["revision_needed"] == 1
    assert result["high_grade"] == 0
