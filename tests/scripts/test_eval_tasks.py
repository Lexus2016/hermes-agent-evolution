"""Tests for the eval harness task-set schema and loader (issue #1514).

Verifies:
- ``EvalTask`` dataclass fields, defaults, and ``compiled_pattern()``.
- ``load_task_set()`` returns the correct tasks for a version.
- v1.0 has 5-10 tasks, ≥3 categories, and ≥3 difficulty levels.
- Versioning: ``list_versions()``, ``latest_version()``, unknown version raises.
- Helper functions: ``task_count()``, ``categories()``, ``difficulties()``.
"""

import re
import unittest

from scripts.eval_tasks import (
    EvalTask,
    load_task_set,
    list_versions,
    latest_version,
    task_count,
    categories,
    difficulties,
)


class TestEvalTaskSchema(unittest.TestCase):
    """EvalTask dataclass — fields, defaults, compiled_pattern()."""

    def test_minimal_task(self):
        task = EvalTask(id="test-001", prompt="Hello")
        self.assertEqual(task.id, "test-001")
        self.assertEqual(task.prompt, "Hello")
        self.assertEqual(task.expected_tools, [])
        self.assertIsNone(task.expected_result_pattern)
        self.assertEqual(task.category, "reasoning")
        self.assertEqual(task.difficulty, "medium")
        self.assertEqual(task.version, "1.0")
        self.assertEqual(task.notes, "")

    def test_full_task(self):
        task = EvalTask(
            id="test-002",
            prompt="Write a function",
            expected_tools=["terminal", "read_file"],
            expected_result_pattern=r"def\s+\w+",
            category="code",
            difficulty="hard",
            version="1.0",
            notes="Tests code gen",
        )
        self.assertEqual(task.id, "test-002")
        self.assertEqual(task.expected_tools, ["terminal", "read_file"])
        self.assertEqual(task.category, "code")
        self.assertEqual(task.difficulty, "hard")

    def test_compiled_pattern_none(self):
        task = EvalTask(id="t", prompt="p")
        self.assertIsNone(task.compiled_pattern())

    def test_compiled_pattern_returns_regex(self):
        task = EvalTask(
            id="t", prompt="p",
            expected_result_pattern=r"\d+",
        )
        pattern = task.compiled_pattern()
        self.assertIsNotNone(pattern)
        self.assertTrue(pattern.search("abc123"))

    def test_compiled_pattern_case_insensitive(self):
        task = EvalTask(
            id="t", prompt="p",
            expected_result_pattern=r"hello",
        )
        pattern = task.compiled_pattern()
        self.assertTrue(pattern.search("HELLO WORLD"))

    def test_frozen(self):
        """EvalTask is frozen — immutable."""
        task = EvalTask(id="t", prompt="p")
        with self.assertRaises(Exception):
            task.id = "other"


class TestLoadTaskSet(unittest.TestCase):
    """load_task_set() — versioning and task counts."""

    def test_load_default_returns_latest(self):
        tasks = load_task_set()
        self.assertEqual(len(tasks), 8)
        for t in tasks:
            self.assertIsInstance(t, EvalTask)

    def test_load_v1_explicit(self):
        tasks = load_task_set("1.0")
        self.assertEqual(len(tasks), 8)

    def test_load_unknown_version_raises(self):
        with self.assertRaises(ValueError) as ctx:
            load_task_set("99.0")
        self.assertIn("99.0", str(ctx.exception))
        self.assertIn("1.0", str(ctx.exception))

    def test_load_returns_copy(self):
        """load_task_set returns a new list — mutating it doesn't affect the registry."""
        tasks1 = load_task_set()
        tasks1.append(EvalTask(id="fake", prompt="x"))
        tasks2 = load_task_set()
        self.assertEqual(len(tasks2), 8)

    def test_task_count_default(self):
        self.assertEqual(task_count(), 8)

    def test_task_count_v1(self):
        self.assertEqual(task_count("1.0"), 8)


class TestV1Requirements(unittest.TestCase):
    """v1.0 task set meets the issue requirements: 5-10 tasks, ≥3 categories."""

    def setUp(self):
        self.tasks = load_task_set("1.0")

    def test_task_count_in_range(self):
        """Issue requires 5-10 tasks."""
        self.assertGreaterEqual(len(self.tasks), 5)
        self.assertLessEqual(len(self.tasks), 10)

    def test_at_least_3_categories(self):
        """Issue requires ≥3 tool categories."""
        cats = categories("1.0")
        self.assertGreaterEqual(len(cats), 3)

    def test_categories_are_expected(self):
        cats = set(categories("1.0"))
        self.assertIn("code", cats)
        self.assertIn("search", cats)
        self.assertIn("file_ops", cats)
        self.assertIn("reasoning", cats)

    def test_difficulties_present(self):
        diffs = set(difficulties("1.0"))
        self.assertIn("easy", diffs)
        self.assertIn("medium", diffs)

    def test_all_tasks_have_unique_ids(self):
        ids = [t.id for t in self.tasks]
        self.assertEqual(len(ids), len(set(ids)))

    def test_all_tasks_have_prompts(self):
        for t in self.tasks:
            self.assertTrue(t.prompt, f"Empty prompt for {t.id}")

    def test_all_tasks_version_1_0(self):
        for t in self.tasks:
            self.assertEqual(t.version, "1.0")

    def test_code_tasks_expect_tools(self):
        for t in self.tasks:
            if t.category == "code":
                self.assertTrue(t.expected_tools, f"Code task {t.id} has no expected tools")

    def test_reasoning_tasks_no_tools(self):
        for t in self.tasks:
            if t.category == "reasoning":
                self.assertEqual(t.expected_tools, [],
                                 f"Reasoning task {t.id} unexpectedly expects tools")


class TestVersioningFunctions(unittest.TestCase):
    """list_versions(), latest_version() — versioning helpers."""

    def test_list_versions(self):
        versions = list_versions()
        self.assertIn("1.0", versions)
        self.assertEqual(versions, sorted(versions))

    def test_latest_version(self):
        self.assertEqual(latest_version(), "1.0")

    def test_latest_version_in_list(self):
        self.assertIn(latest_version(), list_versions())


if __name__ == "__main__":
    unittest.main()