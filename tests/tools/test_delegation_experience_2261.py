"""Delegation-outcome recording into the experience bank (#2261, Slice A).

Finalizing child results must append one ExperienceEntry per child — with
the delegation configuration (goal, role, model) in ``stats.delegation`` —
to the EXISTING bank, and must never break finalization on bank failure.
"""

import json

import pytest

import tools.delegate_tool as dt
from agent import experience_bank as eb


@pytest.fixture()
def bank_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    return tmp_path


class _Parent:
    session_id = "parent-sess-1"
    _memory_manager = None


def _results():
    return [
        {
            "task_index": 0,
            "status": "completed",
            "summary": "did the thing",
            "model": "glm-4.7",
            "exit_reason": "completed",
            "api_calls": 3,
            "duration_seconds": 12.5,
            "_child_role": "leaf",
            "_child_cost_usd": 0.02,
        },
        {
            "task_index": 1,
            "status": "failed",
            "summary": "",
            "model": None,
            "exit_reason": "max_iterations",
            "api_calls": 9,
            "duration_seconds": 30.0,
            "_child_role": None,
            "_child_cost_usd": 0.0,
        },
    ]


def _tasks():
    return [
        {"goal": "analyze the repo map"},
        {"goal": "write the summary"},
    ]


def test_finalize_records_one_entry_per_child(bank_home):
    dt._finalize_child_results(_results(), _tasks(), [], _Parent())
    entries = list(eb.iter_entries())
    assert len(entries) == 2
    by_platform = {e.platform: e for e in entries}
    assert set(by_platform) == {"delegation"}


def test_entry_carries_delegation_configuration(bank_home):
    dt._finalize_child_results(_results(), _tasks(), [], _Parent())
    e = next(e for e in eb.iter_entries() if e.success is True)
    d = e.stats["delegation"]
    assert d["goal"] == "analyze the repo map"
    assert d["role"] == "leaf"
    assert d["status"] == "completed"
    assert d["api_calls"] == 3
    assert d["cost_usd"] == 0.02
    assert e.model == "glm-4.7"
    assert e.primary_dimension == "orchestration"
    assert e.outcome_source == "heuristic:child_status"


def test_failed_child_recorded_with_success_false(bank_home):
    dt._finalize_child_results(_results(), _tasks(), [], _Parent())
    e = next(e for e in eb.iter_entries() if e.success is False)
    assert e.stats["delegation"]["status"] == "failed"
    assert e.terminal_reason == "max_iterations"


def test_bank_failure_never_breaks_finalization(bank_home, monkeypatch):
    # Point the bank at an unwritable HERMES_HOME after import — append must
    # swallow the error, and finalization must still complete.
    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(eb, "append_entry", boom)
    dt._finalize_child_results(_results(), _tasks(), [], _Parent())  # no raise


def test_out_of_range_task_index_skipped(bank_home):
    results = _results() + [{"task_index": 99, "status": "completed"}]
    dt._finalize_child_results(results, _tasks(), [], _Parent())
    assert len(list(eb.iter_entries())) == 2


def test_entry_survives_bank_round_trip(bank_home):
    dt._finalize_child_results(_results(), _tasks(), [], _Parent())
    raw = json.loads(eb.entries_path().read_text(encoding="utf-8").splitlines()[0])
    e2 = eb.ExperienceEntry.from_dict(raw)
    assert e2.stats["delegation"]["goal"] == "analyze the repo map"
