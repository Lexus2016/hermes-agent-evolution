#!/usr/bin/env python3
"""Unit tests for the evolution eval task set (#1514)."""

from __future__ import annotations

import pytest

from scripts.evolution_eval_tasks import (
    TASK_SET_VERSION,
    EvalTask,
    available_versions,
    load_task_set,
    task_to_dict,
)


class TestEvalTaskSchema:
    def test_defaults(self):
        t = EvalTask(id="x", prompt="do something")
        assert t.expected_tools == []
        assert t.difficulty == "easy"

    def test_frozen(self):
        t = EvalTask(id="x", prompt="p")
        with pytest.raises((AttributeError, TypeError)):
            t.id = "y"  # type: ignore[misc]


class TestLoadTaskSet:
    def test_default_version(self):
        assert load_task_set() == load_task_set(TASK_SET_VERSION)

    def test_v1_has_5_to_10_tasks(self):
        assert 5 <= len(load_task_set("1.0")) <= 10

    def test_returns_copy(self):
        t1 = load_task_set("1.0")
        n = len(t1)
        t1.append(EvalTask(id="fake", prompt="x"))
        assert len(load_task_set("1.0")) == n

    def test_unknown_version_raises(self):
        with pytest.raises(KeyError, match="Unknown task-set version"):
            load_task_set("99.99")

    def test_all_tasks_valid(self):
        for t in load_task_set("1.0"):
            assert t.id and t.prompt and t.category
            assert t.difficulty in ("easy", "medium", "hard")

    def test_unique_ids(self):
        ids = [t.id for t in load_task_set("1.0")]
        assert len(ids) == len(set(ids))

    def test_covers_3_plus_categories(self):
        assert len({t.category for t in load_task_set("1.0")}) >= 3

    def test_task_to_dict(self):
        d = task_to_dict(EvalTask("rt", "roundtrip", ["a"], "test", "medium", "pat"))
        assert d == {
            "id": "rt",
            "prompt": "roundtrip",
            "expected_tools": ["a"],
            "category": "test",
            "difficulty": "medium",
            "expected_result_pattern": "pat",
        }


def test_v1_registered_and_sorted():
    assert "1.0" in available_versions()
    assert available_versions() == sorted(available_versions())
