"""Tests for search_files empty-result spiral detection (#1372).

When search_files returns 0 results repeatedly (diverse-query spiral),
the tool should inject a ``_search_directive`` after a threshold telling
the agent to switch strategy. This addresses the regression of #1149
where the spiral_failure_cap only counts errors, not empty results.
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tools.file_tools import search_tool, _read_tracker, _read_tracker_lock


@pytest.fixture(autouse=True)
def _clear_tracker():
    """Ensure each test starts with a clean empty-search counter."""
    with _read_tracker_lock:
        _read_tracker.clear()
    yield
    with _read_tracker_lock:
        _read_tracker.clear()


_SEARCH_PATTERN = chr(0x1) + "NOMATCH" + chr(0x2)  # unmatchable control chars


def _parse_result(raw: str) -> dict:
    """Parse search_tool output, handling appended hint text."""
    # search_tool may append "\n\n[Hint: ...]" after the JSON.
    return json.loads(raw.split("\n\n[Hint:")[0])


def _search_empty(task_id="test-1372", pattern=None):
    """Run a search that will return 0 results."""
    return _parse_result(
        search_tool(
            pattern=pattern or _SEARCH_PATTERN,
            target="content",
            path=".",
            task_id=task_id,
        )
    )


class TestSearchFilesEmptySpiral:
    """Verify the empty-result counter and directive injection."""

    def test_first_empty_no_directive(self):
        """A single empty result should NOT trigger the directive."""
        result = _search_empty()
        assert result.get("total_count") == 0
        assert "_search_directive" not in result

    def test_directive_fires_after_threshold(self):
        """After 3+ cumulative empty results, directive should be present."""
        for i in range(2):
            _search_empty(pattern=_SEARCH_PATTERN + str(i))
        result = _search_empty(pattern=_SEARCH_PATTERN + "final")  # 3rd empty
        assert "_search_directive" in result, (
            f"Expected _search_directive after 3 empties, got keys: {list(result.keys())}"
        )
        directive = result["_search_directive"]
        assert "3 times" in directive
        assert "SWITCH STRATEGY" in directive

    def test_directive_counts_cumulative(self):
        """4th empty should show updated count."""
        for i in range(3):
            _search_empty(pattern=_SEARCH_PATTERN + str(i))
        result = _search_empty(pattern=_SEARCH_PATTERN + "fin")  # 4th empty
        assert "_search_directive" in result
        assert "4 times" in result["_search_directive"]

    def test_different_patterns_all_count(self):
        """Empty results from DIFFERENT queries should all increment."""
        for i in range(3):
            _search_empty(pattern=_SEARCH_PATTERN + chr(65 + i))
        # 4th different query, still empty
        result = _search_empty(pattern=_SEARCH_PATTERN + "Z")
        assert "_search_directive" in result

    def test_counter_resets_on_success(self):
        """A successful search with results should reset the empty counter."""
        # Two empties
        _search_empty(pattern=_SEARCH_PATTERN + "A")
        _search_empty(pattern=_SEARCH_PATTERN + "B")
        # A successful search (find a real file)
        success = _parse_result(
            search_tool(
                pattern="*.py",
                target="files",
                path=".",
                task_id="test-reset",
            )
        )
        assert success.get("total_count", 0) > 0, "Test setup: need *.py files"
        # Now two more empties — should only be at 2, not 4
        _search_empty(pattern=_SEARCH_PATTERN + "C", task_id="test-reset")
        result = _search_empty(pattern=_SEARCH_PATTERN + "D", task_id="test-reset")
        assert "_search_directive" not in result, (
            "Counter should have reset after a successful search"
        )
