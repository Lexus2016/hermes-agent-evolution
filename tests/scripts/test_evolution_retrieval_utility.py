#!/usr/bin/env python3
"""Tests for retrieval-utility logging + history-based deletion (#1480)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(_SCRIPTS))

from evolution_retrieval_utility import (  # noqa: E402
    DEFAULT_MIN_RETRIEVALS,
    DEFAULT_UTILITY_THRESHOLD,
    DeletionCandidate,
    UtilityRecord,
    compute_deletion_candidates,
    load_utility_log,
    log_retrieval,
    update_outcome,
)


def test_log_retrieval_writes_jsonl(tmp_path: Path) -> None:
    """log_retrieval appends one JSON line per call."""
    log_retrieval("rec-1", "heuristic", task_key="tk1", evolution_dir=tmp_path)
    log_retrieval("rec-2", "heuristic", task_key="tk2", evolution_dir=tmp_path)

    path = tmp_path / "retrieval-utility.jsonl"
    assert path.exists()
    lines = path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2

    d = json.loads(lines[0])
    assert d["record_id"] == "rec-1"
    assert d["record_type"] == "heuristic"
    assert d["task_key"] == "tk1"


def test_log_retrieval_empty_id_is_noop(tmp_path: Path) -> None:
    """Empty record_id must not write."""
    result = log_retrieval("", "heuristic", evolution_dir=tmp_path)
    assert result is None
    assert not (tmp_path / "retrieval-utility.jsonl").exists()


def test_log_retrieval_outcome_none_omitted(tmp_path: Path) -> None:
    """When outcome is None (unknown), the key is omitted from the JSON line."""
    log_retrieval("rec-1", outcome=None, evolution_dir=tmp_path)
    lines = (
        (tmp_path / "retrieval-utility.jsonl")
        .read_text(encoding="utf-8")
        .strip()
        .splitlines()
    )
    d = json.loads(lines[0])
    assert "outcome" not in d


def test_log_retrieval_outcome_bool_written(tmp_path: Path) -> None:
    """When outcome is True/False, it is written."""
    log_retrieval("rec-1", outcome=True, evolution_dir=tmp_path)
    log_retrieval("rec-2", outcome=False, evolution_dir=tmp_path)
    lines = (
        (tmp_path / "retrieval-utility.jsonl")
        .read_text(encoding="utf-8")
        .strip()
        .splitlines()
    )
    assert json.loads(lines[0])["outcome"] is True
    assert json.loads(lines[1])["outcome"] is False


def test_load_utility_log_missing_file(tmp_path: Path) -> None:
    """Missing file returns [], not raises."""
    assert load_utility_log(tmp_path) == []


def test_load_utility_log_skips_malformed(tmp_path: Path) -> None:
    """Malformed lines are skipped, valid ones kept."""
    path = tmp_path / "retrieval-utility.jsonl"
    lines = [
        json.dumps({"record_id": "a", "record_type": "h"}),
        "not json",
        json.dumps({"record_id": "b", "record_type": "h", "outcome": True}),
        "",
        '{"no_record_id": true}',
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    records = load_utility_log(tmp_path)
    assert len(records) == 2
    assert records[0].record_id == "a"
    assert records[1].record_id == "b"
    assert records[1].outcome is True


def test_update_outcome_backfills_pending(tmp_path: Path) -> None:
    """update_outcome fills None outcomes for matching task_key."""
    log_retrieval("rec-1", task_key="tk1", outcome=None, evolution_dir=tmp_path)
    log_retrieval("rec-2", task_key="tk1", outcome=None, evolution_dir=tmp_path)
    log_retrieval("rec-3", task_key="tk2", outcome=None, evolution_dir=tmp_path)

    updated = update_outcome("tk1", True, evolution_dir=tmp_path)
    assert updated == 2

    records = load_utility_log(tmp_path)
    outcomes = {r.record_id: r.outcome for r in records}
    assert outcomes["rec-1"] is True
    assert outcomes["rec-2"] is True
    assert outcomes["rec-3"] is None  # different task_key


def test_update_outcome_skips_already_set(tmp_path: Path) -> None:
    """update_outcome does not overwrite an already-recorded outcome."""
    log_retrieval("rec-1", task_key="tk1", outcome=False, evolution_dir=tmp_path)
    updated = update_outcome("tk1", True, evolution_dir=tmp_path)
    assert updated == 0
    records = load_utility_log(tmp_path)
    assert records[0].outcome is False


def test_compute_deletion_candidates_basic() -> None:
    """Records below threshold with enough retrievals are candidates."""
    records = [
        UtilityRecord(record_id="good", record_type="h", outcome=True),
        UtilityRecord(record_id="good", record_type="h", outcome=True),
        UtilityRecord(record_id="good", record_type="h", outcome=True),
        UtilityRecord(record_id="bad", record_type="h", outcome=False),
        UtilityRecord(record_id="bad", record_type="h", outcome=False),
        UtilityRecord(record_id="bad", record_type="h", outcome=True),
    ]
    candidates = compute_deletion_candidates(
        records, min_retrievals=3, utility_threshold=0.5
    )
    assert len(candidates) == 1
    assert candidates[0].record_id == "bad"
    assert candidates[0].avg_utility == pytest.approx(1 / 3)
    assert candidates[0].retrieval_count == 3


def test_compute_deletion_candidates_excludes_unknown_outcome() -> None:
    """None outcomes don't count toward scored utility."""
    records = [
        UtilityRecord(record_id="r", record_type="h", outcome=None),
        UtilityRecord(record_id="r", record_type="h", outcome=None),
        UtilityRecord(record_id="r", record_type="h", outcome=False),
    ]
    # 3 total retrievals, but only 1 scored. avg = 0/1 = 0 < 0.5 → candidate
    candidates = compute_deletion_candidates(
        records, min_retrievals=3, utility_threshold=0.5
    )
    assert len(candidates) == 1
    assert candidates[0].scored_count == 1


def test_compute_deletion_candidates_below_min_retrievals() -> None:
    """Records with too few retrievals are not candidates."""
    records = [
        UtilityRecord(record_id="r", record_type="h", outcome=False),
        UtilityRecord(record_id="r", record_type="h", outcome=False),
    ]
    candidates = compute_deletion_candidates(records, min_retrievals=3)
    assert candidates == []


def test_compute_deletion_candidates_all_none_outcome() -> None:
    """Records with zero scored retrievals produce no candidates."""
    records = [
        UtilityRecord(record_id="r", record_type="h", outcome=None),
        UtilityRecord(record_id="r", record_type="h", outcome=None),
        UtilityRecord(record_id="r", record_type="h", outcome=None),
    ]
    candidates = compute_deletion_candidates(records, min_retrievals=3)
    assert candidates == []


def test_compute_deletion_candidates_sorted_worst_first() -> None:
    """Candidates are sorted by lowest utility first."""
    records = []
    # mid: 1/3 = 0.33
    for _ in range(2):
        records.append(UtilityRecord(record_id="mid", record_type="h", outcome=False))
    records.append(UtilityRecord(record_id="mid", record_type="h", outcome=True))
    # worst: 0/3 = 0.0
    for _ in range(3):
        records.append(UtilityRecord(record_id="worst", record_type="h", outcome=False))

    candidates = compute_deletion_candidates(records, min_retrievals=3)
    assert candidates[0].record_id == "worst"
    assert candidates[1].record_id == "mid"


def test_compute_deletion_candidates_separates_by_type() -> None:
    """Same record_id, different record_type are tracked separately."""
    records = [
        UtilityRecord(record_id="r", record_type="heuristic", outcome=False),
        UtilityRecord(record_id="r", record_type="heuristic", outcome=False),
        UtilityRecord(record_id="r", record_type="heuristic", outcome=False),
        UtilityRecord(record_id="r", record_type="skill", outcome=True),
        UtilityRecord(record_id="r", record_type="skill", outcome=True),
        UtilityRecord(record_id="r", record_type="skill", outcome=True),
    ]
    candidates = compute_deletion_candidates(records, min_retrievals=3)
    assert len(candidates) == 1
    assert candidates[0].record_type == "heuristic"


def test_utility_record_from_dict_normalizes_bad_outcome() -> None:
    """Non-bool outcome in JSON is coerced to None, not truthy."""
    rec = UtilityRecord.from_dict({"record_id": "x", "outcome": "maybe"})
    assert rec.outcome is None


def test_apply_deletions_calls_callbacks() -> None:
    """apply_deletions invokes loader+deleter for matching record_type."""
    from evolution_retrieval_utility import apply_deletions

    candidates = [
        DeletionCandidate(
            record_id="a",
            record_type="heuristic",
            retrieval_count=3,
            scored_count=3,
            avg_utility=0.0,
        ),
        DeletionCandidate(
            record_id="b",
            record_type="skill",
            retrieval_count=3,
            scored_count=3,
            avg_utility=0.0,
        ),
    ]
    deleted_ids: list[str] = []
    apply_deletions(
        candidates,
        record_type="heuristic",
        loader=lambda rid: {"id": rid},
        deleter=lambda rid: deleted_ids.append(rid) or True,
    )
    assert deleted_ids == ["a"]


def test_apply_deletions_skips_absent() -> None:
    """apply_deletions does not call deleter when loader returns None."""
    from evolution_retrieval_utility import apply_deletions

    candidates = [
        DeletionCandidate(
            record_id="a",
            record_type="heuristic",
            retrieval_count=3,
            scored_count=3,
            avg_utility=0.0,
        ),
    ]
    deleted = apply_deletions(
        candidates,
        record_type="heuristic",
        loader=lambda rid: None,
        deleter=lambda rid: True,
    )
    assert deleted == []


def test_retrieve_logs_to_utility_store(tmp_path: Path) -> None:
    """evolution_heuristic_retrieve.retrieve logs retrievals to utility log."""
    from evolution_heuristic_retrieve import retrieve

    heuristics = [
        {
            "text": "use type hints",
            "task_type": "coding",
            "pattern": ["a", "b"],
            "outcome_score": 0.8,
            "frequency": 5,
        },
        {
            "text": "check logs",
            "task_type": "ops",
            "pattern": ["c"],
            "outcome_score": 0.6,
            "frequency": 3,
        },
    ]
    ranked = retrieve(
        "write a function",
        heuristics,
        top_k=2,
        task_type="coding",
        task_key="test-task-key",
        evolution_dir=tmp_path,
    )
    assert len(ranked) == 2

    # Utility log should have one entry per retrieved heuristic.
    records = load_utility_log(tmp_path)
    assert len(records) == 2
    assert all(r.task_key == "test-task-key" for r in records)
    assert all(r.record_type == "heuristic" for r in records)


def test_retrieve_no_task_key_skips_logging(tmp_path: Path) -> None:
    """Without task_key, retrieval does not log utility (backward compat)."""
    from evolution_heuristic_retrieve import retrieve

    heuristics = [{"text": "x", "task_type": "t", "pattern": [], "outcome_score": 0.5}]
    retrieve("ctx", heuristics, top_k=1)
    # No evolution_dir passed → writes to default location, not tmp_path.
    # Verify nothing was written to tmp_path.
    assert not (tmp_path / "retrieval-utility.jsonl").exists()
