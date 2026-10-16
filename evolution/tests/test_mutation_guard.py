"""Tests for :mod:`evolution.lib.mutation_guard` (issue #3014, counter slice)."""

from __future__ import annotations

import json

import pytest

from evolution.lib.mutation_guard import (
    FailureCauseSummary,
    FileSnapshot,
    MutatingFailureCounter,
)


class TestMutatingFailureCounter:
    def test_record_and_summary(self, tmp_path):
        ledger = tmp_path / "ledger.jsonl"
        counter = MutatingFailureCounter(ledger)
        counter.record("run-1", "mutating")
        counter.record("run-2", "other")
        counter.record("run-3", "mutating")
        s = counter.summary()
        assert s.failed_runs == 3
        assert s.mutating_cause == 2
        assert s.other_cause == 1
        assert s.mutating_share == round(2 / 3, 3)

    def test_no_records_empty_summary(self, tmp_path):
        counter = MutatingFailureCounter(tmp_path / "nope.jsonl")
        s = counter.summary()
        assert s.failed_runs == 0
        assert s.mutating_cause == 0
        assert s.mutating_share is None

    def test_invalid_cause_rejected(self, tmp_path):
        counter = MutatingFailureCounter(tmp_path / "ledger.jsonl")
        with pytest.raises(ValueError):
            counter.record("run-x", "maybe")

    def test_ledger_lines_are_json(self, tmp_path):
        ledger = tmp_path / "ledger.jsonl"
        counter = MutatingFailureCounter(ledger)
        counter.record("run-1", "mutating", recorded_at="2026-08-21")
        lines = ledger.read_text(encoding="utf-8").strip().splitlines()
        rec = json.loads(lines[0])
        assert rec["run_id"] == "run-1"
        assert rec["cause_category"] == "mutating"
        assert rec["outcome"] == "failed"
        assert rec["recorded_at"] == "2026-08-21"

    def test_malformed_lines_skipped(self, tmp_path):
        ledger = tmp_path / "ledger.jsonl"
        ledger.write_text("not-json\n", encoding="utf-8")
        counter = MutatingFailureCounter(ledger)
        counter.record("run-1", "other")
        s = counter.summary()
        assert s.failed_runs == 1
        assert s.mutating_cause == 0


class TestFailureCauseSummary:
    def test_defaults(self):
        s = FailureCauseSummary()
        assert s.failed_runs == 0
        assert s.mutating_share is None

    def test_to_dict_plain_types(self):
        s = FailureCauseSummary(
            failed_runs=4, mutating_cause=1, other_cause=3, mutating_share=0.25
        )
        d = s.to_dict()
        assert d["failed_runs"] == 4
        assert d["mutating_share"] == 0.25
        json.dumps(d)  # must not raise


class TestFileSnapshot:
    def test_snapshot_and_restore_modified_file(self, tmp_path):
        f = tmp_path / "foo.txt"
        f.write_text("initial content", encoding="utf-8")
        snapshot = FileSnapshot([f])
        assert not snapshot.has_changed()

        # Mutate
        f.write_text("corrupted content", encoding="utf-8")
        assert snapshot.has_changed()

        # Restore
        restored = snapshot.restore()
        assert restored == 1
        assert f.read_text(encoding="utf-8") == "initial content"
        assert not snapshot.has_changed()

    def test_snapshot_and_restore_deleted_file(self, tmp_path):
        f = tmp_path / "bar.txt"
        f.write_text("must survive", encoding="utf-8")
        snapshot = FileSnapshot([f])

        # Delete
        f.unlink()
        assert snapshot.has_changed()

        # Restore
        restored = snapshot.restore()
        assert restored == 1
        assert f.exists()
        assert f.read_text(encoding="utf-8") == "must survive"

    def test_snapshot_and_restore_created_file(self, tmp_path):
        f = tmp_path / "new_file.txt"
        assert not f.exists()
        snapshot = FileSnapshot([f])
        assert not snapshot.has_changed()

        # Create
        f.write_text("newly created", encoding="utf-8")
        assert snapshot.has_changed()

        # Restore (should remove newly created file)
        restored = snapshot.restore()
        assert restored == 1
        assert not f.exists()
        assert not snapshot.has_changed()

    def test_context_manager_rollback_on_exception(self, tmp_path):
        f = tmp_path / "target.txt"
        f.write_text("original", encoding="utf-8")

        with pytest.raises(RuntimeError):
            with FileSnapshot([f]):
                f.write_text("bad mutation", encoding="utf-8")
                raise RuntimeError("mutation failed!")

        # Should be auto-reverted by context manager
        assert f.read_text(encoding="utf-8") == "original"

    def test_context_manager_no_rollback_on_success(self, tmp_path):
        f = tmp_path / "target.txt"
        f.write_text("original", encoding="utf-8")

        with FileSnapshot([f]):
            f.write_text("successful mutation", encoding="utf-8")

        # Kept because no exception was raised
        assert f.read_text(encoding="utf-8") == "successful mutation"

