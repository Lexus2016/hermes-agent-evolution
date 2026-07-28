"""Tests for per-session read_file failure-rate directive (#1370).

When read_file fails repeatedly (file-not-found, path hallucination), the
tool should inject a ``_rate_directive`` after a threshold so the agent
stops guessing paths and switches to search_files / repo_map.
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tools.file_tools import read_file_tool, _read_tracker, _read_tracker_lock


@pytest.fixture(autouse=True)
def _clear_tracker():
    """Ensure each test starts with a clean read-failure counter."""
    with _read_tracker_lock:
        _read_tracker.clear()
    yield
    with _read_tracker_lock:
        _read_tracker.clear()


def _read_missing(task_id="test-1370", idx=0):
    """Call read_file on a path that doesn't exist."""
    return json.loads(
        read_file_tool(
            f"/nonexistent/path/ghost_file_{idx}.py", task_id=task_id
        )
    )


class TestReadFileFailureRateDirective:
    """Verify the cumulative failure counter and directive injection."""

    def test_first_failure_no_directive(self):
        """A single failure should NOT trigger the directive."""
        result = _read_missing(idx=0)
        assert result.get("error") or result.get("error_class")
        assert "_rate_directive" not in result

    def test_directive_fires_after_threshold(self):
        """After 4+ cumulative failures, directive should be present."""
        for i in range(3):
            _read_missing(idx=i)
        result = _read_missing(idx=99)  # 4th failure
        assert "_rate_directive" in result, (
            f"Expected _rate_directive after 4 failures, got keys: {list(result.keys())}"
        )
        directive = result["_rate_directive"]
        assert "4 times" in directive
        assert "search_files" in directive or "repo_map" in directive

    def test_directive_counts_cumulative(self):
        """5th failure should show updated count."""
        for i in range(4):
            _read_missing(idx=i)
        result = _read_missing(idx=99)  # 5th failure
        assert "_rate_directive" in result
        assert "5 times" in result["_rate_directive"]

    def test_different_paths_all_count(self):
        """Failures on DIFFERENT paths should all increment the same counter."""
        paths = [
            "/nonexistent/a.py",
            "/nonexistent/b.py",
            "/nonexistent/c.py",
            "/nonexistent/d.py",
        ]
        for p in paths:
            r = json.loads(read_file_tool(p, task_id="test-diff"))
        # 4th failure should trigger directive
        r4 = json.loads(read_file_tool("/nonexistent/e.py", task_id="test-diff"))
        assert "_rate_directive" in r4

    def test_counter_is_per_session(self):
        """Different task_ids should have independent counters."""
        for _ in range(3):
            _read_missing(task_id="session-A")
        # session-B has 0 failures so far
        result_b = _read_missing(task_id="session-B")
        assert "_rate_directive" not in result_b, (
            "Counter leaked across sessions — should be per task_id"
        )
