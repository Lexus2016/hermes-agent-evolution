# -*- coding: utf-8 -*-
"""Tests for :mod:`agent.delegation_bank` — delegation pattern bank (#2261).

Hermetic by construction: the autouse fixtures in ``tests/conftest.py``
redirect ``HERMES_HOME`` to a per-test tempdir, and the module resolves
``get_hermes_home()`` lazily on every call, so no per-test monkeypatching
is needed. Stdlib + pytest only; behavior/invariant tests.
"""

from __future__ import annotations

import json
import os
import time

import pytest

from agent import delegation_bank as db
from agent.delegation_bank import (
    DelegationPattern,
    DelegationRecord,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _record(**overrides) -> DelegationRecord:
    base = dict(
        ts=1_700_000_000.0,
        goal="Analyze the test coverage of module X",
        role="leaf",
        model="test-model",
        handoff_mode=None,
        max_iterations=None,
        success=True,
        status="completed",
        duration_seconds=10.0,
        summary="Found 3 untested functions.",
        api_calls=5,
        session_id="sess-1",
    )
    base.update(overrides)
    return DelegationRecord(**base)


# ---------------------------------------------------------------------------
# DelegationRecord serialization
# ---------------------------------------------------------------------------


class TestDelegationRecordSerialization:
    """Round-trip and defensive-deserialization guarantees."""

    def test_roundtrip(self):
        rec = _record(goal="Custom goal", role="orchestrator", model="gpt-4")
        d = rec.to_dict()
        restored = DelegationRecord.from_dict(d)
        assert restored.goal == "Custom goal"
        assert restored.role == "orchestrator"
        assert restored.model == "gpt-4"
        assert restored.success is True

    def test_from_dict_tolerates_missing_fields(self):
        restored = DelegationRecord.from_dict({"ts": 1000.0, "goal": "hi"})
        assert restored.goal == "hi"
        assert restored.role == "leaf"
        assert restored.success is False  # default
        assert restored.status == "failed"  # default

    def test_from_dict_coerces_bad_types(self):
        restored = DelegationRecord.from_dict({
            "ts": "not-a-number",
            "goal": 12345,
            "success": "yes",
            "api_calls": "ten",
            "max_iterations": "fast",
        })
        assert restored.ts == 0.0
        assert "12345" in restored.goal
        assert restored.success is True  # truthy string -> bool
        assert restored.api_calls == 0
        assert restored.max_iterations is None  # non-int -> None

    def test_to_dict_produces_json_serializable(self):
        d = _record().to_dict()
        json.dumps(d)  # must not raise


# ---------------------------------------------------------------------------
# record_delegation — auto-derives signature + config_hash
# ---------------------------------------------------------------------------


class TestRecordDelegation:
    """record_delegation fills task_signature and config_hash automatically."""

    def test_auto_fills_signature_and_hash(self):
        rec = _record(goal="Write a blog post about cats")
        assert rec.task_signature == ""
        assert rec.config_hash == ""
        assert db.record_delegation(rec) is True
        assert rec.task_signature == "write a blog post about cats"
        assert len(rec.config_hash) == 16

    def test_config_hash_stable_for_same_config(self):
        r1 = _record(goal="Task A", role="leaf", model="m1")
        r2 = _record(goal="Task B", role="leaf", model="m1")
        db.record_delegation(r1)
        db.record_delegation(r2)
        assert r1.config_hash == r2.config_hash

    def test_config_hash_differs_for_different_role(self):
        r1 = _record(role="leaf")
        r2 = _record(role="orchestrator")
        db.record_delegation(r1)
        db.record_delegation(r2)
        assert r1.config_hash != r2.config_hash

    def test_record_appended_to_jsonl(self):
        rec = _record(goal="First delegation")
        db.record_delegation(rec)
        assert db.records_path().exists()
        lines = db.records_path().read_text(encoding="utf-8").strip().split("\n")
        assert len(lines) == 1
        parsed = json.loads(lines[0])
        assert parsed["goal"] == "First delegation"


# ---------------------------------------------------------------------------
# iter_records
# ---------------------------------------------------------------------------


class TestIterRecords:
    """Reading records back, including corruption tolerance."""

    def test_empty_returns_nothing(self):
        assert list(db.iter_records()) == []

    def test_iter_returns_newest_first(self):
        db.record_delegation(_record(ts=1000.0, goal="Old"))
        db.record_delegation(_record(ts=2000.0, goal="New"))
        recs = list(db.iter_records())
        assert len(recs) == 2
        assert recs[0].goal == "New"
        assert recs[1].goal == "Old"

    def test_since_ts_filter(self):
        db.record_delegation(_record(ts=1000.0, goal="Old"))
        db.record_delegation(_record(ts=2000.0, goal="New"))
        recs = list(db.iter_records(since_ts=1500.0))
        assert len(recs) == 1
        assert recs[0].goal == "New"

    def test_corrupt_line_skipped(self):
        db.record_delegation(_record(goal="Good record"))
        # Append a corrupt line.
        with open(db.records_path(), "a", encoding="utf-8") as fh:
            fh.write("NOT VALID JSON\n")
        recs = list(db.iter_records())
        assert len(recs) == 1
        assert recs[0].goal == "Good record"


# ---------------------------------------------------------------------------
# promote_patterns
# ---------------------------------------------------------------------------


class TestPromotePatterns:
    """Promotion: only consistently-successful configs become patterns."""

    def test_promote_requires_threshold(self):
        # Only one success — below threshold of 2.
        db.record_delegation(_record(goal="Task A", success=True))
        patterns = db.promote_patterns()
        assert patterns == []

    def test_promote_successful_config(self):
        db.record_delegation(_record(goal="Task A", success=True, model="m1"))
        db.record_delegation(_record(goal="Task A", success=True, model="m1"))
        patterns = db.promote_patterns()
        assert len(patterns) == 1
        p = patterns[0]
        assert p.success_count == 2
        assert p.failure_count == 0
        assert p.model == "m1"

    def test_failures_counted_but_not_promoted_alone(self):
        db.record_delegation(_record(goal="Task A", success=False, status="failed"))
        db.record_delegation(_record(goal="Task A", success=False, status="failed"))
        patterns = db.promote_patterns()
        assert patterns == []

    def test_mixed_records_promote_with_failure_count(self):
        db.record_delegation(_record(goal="Task A", success=True))
        db.record_delegation(_record(goal="Task A", success=True))
        db.record_delegation(_record(goal="Task A", success=False, status="failed"))
        patterns = db.promote_patterns()
        assert len(patterns) == 1
        assert patterns[0].success_count == 2
        assert patterns[0].failure_count == 1

    def test_different_configs_promote_separately(self):
        db.record_delegation(_record(goal="Task A", success=True, role="leaf"))
        db.record_delegation(_record(goal="Task A", success=True, role="leaf"))
        db.record_delegation(_record(goal="Task B", success=True, role="orchestrator"))
        db.record_delegation(_record(goal="Task B", success=True, role="orchestrator"))
        patterns = db.promote_patterns()
        assert len(patterns) == 2

    def test_promote_persists_and_reloadable(self):
        db.record_delegation(_record(goal="Task A", success=True))
        db.record_delegation(_record(goal="Task A", success=True))
        db.promote_patterns()
        reloaded = db.load_delegation_patterns()
        assert len(reloaded) == 1


# ---------------------------------------------------------------------------
# suggest_configurations + format_delegation_suggestions
# ---------------------------------------------------------------------------


class TestSuggestConfigurations:
    """Retrieval: suggest proven configs for a new task."""

    def test_no_patterns_returns_empty(self):
        assert db.suggest_configurations("any goal") == []

    def test_suggests_matching_pattern(self):
        db.record_delegation(_record(goal="Refactor the database layer", success=True))
        db.record_delegation(_record(goal="Refactor the database layer", success=True))
        db.promote_patterns()
        suggestions = db.suggest_configurations("Refactor the database layer now")
        assert len(suggestions) == 1

    def test_no_match_returns_empty(self):
        db.record_delegation(_record(goal="Refactor database", success=True))
        db.record_delegation(_record(goal="Refactor database", success=True))
        db.promote_patterns()
        suggestions = db.suggest_configurations("Write a poem about cats")
        assert suggestions == []

    def test_format_returns_empty_when_no_suggestions(self):
        assert db.format_delegation_suggestions("anything") == ""

    def test_format_renders_block(self):
        db.record_delegation(_record(goal="Refactor database", success=True))
        db.record_delegation(_record(goal="Refactor database", success=True))
        db.promote_patterns()
        text = db.format_delegation_suggestions("Refactor database now")
        assert "Proven delegation configurations" in text
        assert "role=leaf" in text
        assert "succeeded 2×" in text


# ---------------------------------------------------------------------------
# load / save delegation patterns
# ---------------------------------------------------------------------------


class TestLoadSavePatterns:
    """Pattern file persistence and corruption tolerance."""

    def test_load_missing_returns_empty(self):
        assert db.load_delegation_patterns() == []

    def test_save_then_load(self):
        p = DelegationPattern(
            id="delg-abcd1234",
            task_signature="refactor database",
            role="leaf",
            model="m1",
            success_count=3,
        )
        assert db.save_delegation_patterns([p]) is True
        loaded = db.load_delegation_patterns()
        assert len(loaded) == 1
        assert loaded[0].id == "delg-abcd1234"

    def test_load_corrupt_file_returns_empty(self):
        db.patterns_path().parent.mkdir(parents=True, exist_ok=True)
        db.patterns_path().write_text("NOT JSON", encoding="utf-8")
        assert db.load_delegation_patterns() == []

    def test_load_dict_format(self):
        """patterns.json may be wrapped in {"patterns": [...]}."""
        db.patterns_path().parent.mkdir(parents=True, exist_ok=True)
        db.patterns_path().write_text(
            json.dumps({
                "patterns": [
                    DelegationPattern(
                        id="delg-x", task_signature="t", role="leaf"
                    ).to_dict()
                ]
            }),
            encoding="utf-8",
        )
        loaded = db.load_delegation_patterns()
        assert len(loaded) == 1
        assert loaded[0].id == "delg-x"
