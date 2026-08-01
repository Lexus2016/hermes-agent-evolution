"""Tests for scripts/eval_tasks.py — fixed versioned task set (issue #1514).

Asserts the issue's success criteria as invariants, plus the live-consumer wire
(rework brief DoD). Pure stdlib + pytest; no network.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

import pytest  # noqa: E402
from eval_tasks import TASK_SET_VERSION, EvalTask, latest_version, load_task_set  # noqa: E402

REQUIRED_FIELDS = (
    "id",
    "prompt",
    "expected_tools",
    "expected_result_pattern",
    "category",
    "difficulty",
)


class TestSchema:
    def test_evaltask_has_all_required_fields_and_defaults(self):
        t = EvalTask(id="x", prompt="do x")
        assert all(hasattr(t, f) for f in REQUIRED_FIELDS)
        assert t.expected_tools == [] and t.expected_result_pattern is None
        assert t.category == "reasoning" and t.difficulty == "easy"


class TestTaskSetInvariants:
    def test_count_categories_fields_and_unique_ids(self):
        tasks = load_task_set()
        assert 5 <= len(tasks) <= 10
        assert len({t.category for t in tasks}) >= 3  # >=3 tool categories
        ids = []
        for t in tasks:
            assert t.id and t.prompt and t.category
            assert t.difficulty in ("easy", "medium", "hard")
            ids.append(t.id)
        assert len(ids) == len(set(ids))  # unique ids


class TestLoader:
    def test_load_returns_fresh_list_of_evaltasks(self):
        assert load_task_set() is not load_task_set()  # fresh copy each call
        assert all(isinstance(t, EvalTask) for t in load_task_set())

    def test_version_and_explicit_load_match(self):
        assert TASK_SET_VERSION == latest_version()
        assert load_task_set("1.0") == load_task_set(TASK_SET_VERSION)

    def test_unknown_version_raises(self):
        with pytest.raises(KeyError):
            load_task_set("9.9")


class TestLiveConsumerWire:
    """Rework brief DoD: a non-test, non-eval_tasks.py module must consume the
    data layer. evolution_evaluator.py imports it at module load."""

    def test_evaluator_imports_and_asserts_task_set(self):
        import evolution_evaluator

        assert evolution_evaluator._EVAL_TASK_SET_OK is True
