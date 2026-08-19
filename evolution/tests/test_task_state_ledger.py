# -*- coding: utf-8 -*-
"""Tests for :mod:`evolution.lib.task_state_ledger` (issue #2841)."""

from __future__ import annotations

import json

from evolution.lib.task_state_ledger import LedgerEntry, TaskStateLedger


class TestLedgerEntrySerialisation:
    """to_dict / from_dict round-trip."""

    def test_roundtrip_basic(self):
        e = LedgerEntry(
            step="analysis/select",
            verified=True,
            outcome="picked 2 issues",
            evidence=["issues/2026-08-19.json", "abc123"],
            timestamp=1755600000.0,
        )
        e2 = LedgerEntry.from_dict(e.to_dict())
        assert e2.step == e.step
        assert e2.verified is True
        assert e2.outcome == e.outcome
        assert e2.evidence == e.evidence
        assert e2.timestamp == e.timestamp

    def test_roundtrip_defaults(self):
        e2 = LedgerEntry.from_dict(LedgerEntry(step="x").to_dict())
        assert e2.verified is True
        assert e2.outcome == ""
        assert e2.evidence == []
        assert e2.step == "x"

    def test_to_dict_json_serialisable(self):
        json.dumps(LedgerEntry(step="s", outcome="o", evidence=["p"]).to_dict())


class TestTaskStateLedger:
    """Append / read / persist behaviour."""

    def test_append_and_completed(self, tmp_path):
        ledger = TaskStateLedger(task_id="t1", storage_dir=tmp_path, auto_save=False)
        assert ledger.completed("step1") is False
        ledger.append(LedgerEntry(step="step1", outcome="done"))
        assert ledger.completed("step1") is True
        assert ledger.completed("step2") is False
        last = ledger.last_verified_step
        assert last is not None
        assert last.step == "step1"

    def test_empty_ledger(self, tmp_path):
        ledger = TaskStateLedger(task_id="t2", storage_dir=tmp_path, auto_save=False)
        assert ledger.last_verified_step is None
        assert ledger.summary() == "no verified steps recorded"

    def test_persistence_across_instances(self, tmp_path):
        ledger = TaskStateLedger(task_id="t3", storage_dir=tmp_path, auto_save=True)
        ledger.append(
            LedgerEntry(step="impl/write", outcome="added", evidence=["x.py"])
        )
        reloaded = TaskStateLedger(task_id="t3", storage_dir=tmp_path)
        assert reloaded.completed("impl/write") is True
        assert reloaded.entries[0].outcome == "added"

    def test_to_dict_from_dict_roundtrip(self, tmp_path):
        ledger = TaskStateLedger(task_id="t4", storage_dir=tmp_path, auto_save=False)
        ledger.append(LedgerEntry(step="a", verified=False, outcome="o"))
        ledger2 = TaskStateLedger.from_dict(ledger.to_dict())
        assert ledger2.task_id == "t4"
        assert ledger2.entries[0].verified is False
        assert ledger2.completed("a") is True

    def test_load_missing_file_returns_false(self, tmp_path):
        ledger = TaskStateLedger(task_id="t5", storage_dir=tmp_path, auto_save=False)
        assert ledger.load(tmp_path / "nope.json") is False
        assert ledger.entries == []

    def test_summary_lists_verified_steps(self, tmp_path):
        ledger = TaskStateLedger(task_id="t6", storage_dir=tmp_path, auto_save=False)
        ledger.append(LedgerEntry(step="a", outcome="done", evidence=["f1"]))
        ledger.append(LedgerEntry(step="b", verified=False, outcome="partial"))
        text = ledger.summary()
        assert "- a: verified" in text
        assert "- b: unverified" in text
        assert "done" in text
