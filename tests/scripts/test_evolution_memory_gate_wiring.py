"""Wiring tests for evolution_memory_gate.py (#1270).

Verifies the Experience-Following selective memory gate: quality gate
blocks low-quality records, error-propagation guard blocks records
deriving from quarantined sources, history-based deletion flags
low-utility records, and the misaligned detector flags records that pass
quality but fail downstream. Covers invariants, not snapshots.
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

import evolution_memory_gate as mg  # noqa: E402


def _sample_payload() -> dict:
    return {
        "records": [
            # High-quality, clean source -> admitted.
            {
                "record_id": "rec-good",
                "artifact_type": "research_report",
                "quality_score": 0.9,
                "source_record_ids": [],
            },
            # Low-quality -> rejected by quality gate.
            {
                "record_id": "rec-lowq",
                "artifact_type": "issues_file",
                "quality_score": 0.3,
                "source_record_ids": [],
            },
            # High-quality but derives from a quarantined source -> blocked.
            {
                "record_id": "rec-poisoned",
                "artifact_type": "implementation_log",
                "quality_score": 0.95,
                "source_record_ids": ["noisy-rec-1"],
            },
        ],
        "quality_threshold": 0.7,
        "quarantined_ids": ["noisy-rec-1"],
        "retrieval_log": [
            # rec-lowutil retrieved 4 times with avg outcome 0.1 -> deletion candidate.
            {
                "record_id": "rec-lowutil",
                "downstream_outcome": 0.0,
                "retrieval_context": "c1",
            },
            {
                "record_id": "rec-lowutil",
                "downstream_outcome": 0.2,
                "retrieval_context": "c2",
            },
            {
                "record_id": "rec-lowutil",
                "downstream_outcome": 0.1,
                "retrieval_context": "c3",
            },
            {
                "record_id": "rec-lowutil",
                "downstream_outcome": 0.1,
                "retrieval_context": "c4",
            },
            # rec-goodutil retrieved 3 times with avg 0.8 -> NOT a deletion candidate.
            {"record_id": "rec-goodutil", "downstream_outcome": 0.8},
            {"record_id": "rec-goodutil", "downstream_outcome": 0.9},
            {"record_id": "rec-goodutil", "downstream_outcome": 0.7},
            # rec-misaligned: retrieved 2x with avg 0.15 -> misaligned flag (min_retrievals=2).
            {"record_id": "rec-misaligned", "downstream_outcome": 0.1},
            {"record_id": "rec-misaligned", "downstream_outcome": 0.2},
        ],
        "deletion": {"min_retrievals": 3, "utility_threshold": 0.4},
        "misaligned": {"min_retrievals": 2, "outcome_threshold": 0.3},
    }


def test_quality_gate_admits_high_quality():
    report = mg.evaluate(_sample_payload())
    decisions = {d["record_id"]: d for d in report["addition_decisions"]}
    assert decisions["rec-good"]["admitted"] is True
    assert decisions["rec-lowq"]["admitted"] is False
    assert "quality" in decisions["rec-lowq"]["reason"]


def test_error_propagation_guard_blocks_poisoned():
    report = mg.evaluate(_sample_payload())
    decisions = {d["record_id"]: d for d in report["addition_decisions"]}
    assert decisions["rec-poisoned"]["admitted"] is False
    assert decisions["rec-poisoned"]["error_propagation_blocked"] is True
    assert "quarantined" in decisions["rec-poisoned"]["reason"]


def test_history_based_deletion_flags_low_utility():
    report = mg.evaluate(_sample_payload())
    candidates = {c["record_id"]: c for c in report["deletion_candidates"]}
    assert "rec-lowutil" in candidates
    assert candidates["rec-lowutil"]["retrieval_count"] == 4
    assert candidates["rec-lowutil"]["avg_downstream_outcome"] < 0.4
    # High-utility record is NOT a deletion candidate.
    assert "rec-goodutil" not in candidates


def test_misaligned_detector_flags_quality_pass_low_outcome():
    report = mg.evaluate(_sample_payload())
    flags = {f["record_id"]: f for f in report["misaligned_flags"]}
    assert "rec-misaligned" in flags
    assert flags["rec-misaligned"]["retrieval_count"] == 2


def test_summary_counts():
    report = mg.evaluate(_sample_payload())
    s = report["summary"]
    assert s["admitted"] == 1
    assert s["rejected"] == 2
    assert s["error_propagation_blocks"] == 1
    assert s["deletion_candidates"] >= 1


def test_main_returns_zero(tmp_path, capsys):
    payload_file = tmp_path / "payload.json"
    payload_file.write_text(json.dumps(_sample_payload()), encoding="utf-8")
    rc = mg.main(["--payload", str(payload_file)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "addition_decisions" in out
