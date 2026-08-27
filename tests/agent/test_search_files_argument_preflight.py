"""Regression tests for the search_files missing-pattern preflight (#3237)."""

import json
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(__import__("pathlib").Path(__file__).parents[2]))

from agent.tool_executor import _parse_tool_arguments


def _make_agent():
    """Return a bare object that can store counter attributes."""
    return SimpleNamespace()


class TestSearchFilesArgumentPreflight:
    def test_search_files_missing_pattern_returns_correction(self):
        agent = _make_agent()
        args, err = _parse_tool_arguments(
            '{"target": "content", "path": "."}',
            function_name="search_files",
            agent=agent,
        )
        assert args == {}
        assert err is not None
        assert isinstance(err, str)
        payload = json.loads(err)
        assert payload["error"] == "Missing required argument: pattern"
        assert payload["tool"] == "search_files"
        assert payload["missing"] == ["pattern"]
        assert "correction" in payload
        assert "Example:" in payload["message"]
        assert agent._search_files_missing_pattern_count == 1
        assert payload["search_files_missing_pattern_count"] == 1
        assert payload["argument_shape_spiral"] is False

    def test_search_files_with_pattern_is_clean(self):
        agent = _make_agent()
        args, err = _parse_tool_arguments(
            '{"pattern": "*.py", "target": "files", "path": "."}',
            function_name="search_files",
            agent=agent,
        )
        assert err is None
        assert args == {"pattern": "*.py", "target": "files", "path": "."}
        assert not hasattr(agent, "_search_files_missing_pattern_count")

    def test_missing_pattern_counter_increments_per_call(self):
        agent = _make_agent()
        for i in range(1, 5):
            args, err = _parse_tool_arguments(
                "{}",
                function_name="search_files",
                agent=agent,
            )
            assert args == {}
            assert err is not None
            payload = json.loads(err)
            assert payload["search_files_missing_pattern_count"] == i
            expected_spiral = i >= 3
            assert payload["argument_shape_spiral"] is expected_spiral
        assert agent._search_files_missing_pattern_count == 4

    def test_no_agent_still_returns_hint(self):
        args, err = _parse_tool_arguments(
            '{"target": "content"}',
            function_name="search_files",
            agent=None,
        )
        assert args == {}
        payload = json.loads(err)
        assert payload["error"] == "Missing required argument: pattern"
        assert payload["search_files_missing_pattern_count"] == 0
        assert payload["argument_shape_spiral"] is False

    def test_other_tools_not_affected(self):
        agent = _make_agent()
        args, err = _parse_tool_arguments(
            '{"command": "echo hello"}',
            function_name="terminal",
            agent=agent,
        )
        assert err is None
        assert args == {"command": "echo hello"}
        assert not hasattr(agent, "_search_files_missing_pattern_count")

    def test_no_blind_retry_path(self):
        """Malformed args result is returned to the model, not retried."""
        agent = _make_agent()
        args, err = _parse_tool_arguments(
            '{"target": "content"}',
            function_name="search_files",
            agent=agent,
        )
        assert err is not None
        # The executor uses this result as the tool result, so no dispatch occurs.
        assert "pattern" in err.lower()
