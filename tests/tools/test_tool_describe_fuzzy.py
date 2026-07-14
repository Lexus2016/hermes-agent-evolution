"""Tests for fuzzy name matching in tool_describe (#978).

When the model calls ``tool_describe`` with a slightly wrong tool name,
the response should include ``suggestions`` with the closest matches
instead of a bare "not available" error, so the agent can self-correct
without a separate ``tool_search`` round-trip.
"""

import json
from unittest.mock import patch

import pytest

from tools.tool_search import dispatch_tool_describe, _fuzzy_tool_names


# ── _fuzzy_tool_names unit tests ─────────────────────────────────────────


class TestFuzzyToolNames:
    def test_empty_query_returns_empty(self):
        assert _fuzzy_tool_names("", ["a", "b"]) == []

    def test_empty_available_returns_empty(self):
        assert _fuzzy_tool_names("query", []) == []

    def test_exact_substring_match(self):
        result = _fuzzy_tool_names("github", ["github_create_issue", "github_list_repos", "web_search"])
        assert "github_create_issue" in result
        assert "github_list_repos" in result
        assert "web_search" not in result

    def test_substring_match_respects_limit(self):
        result = _fuzzy_tool_names("a", ["a1", "a2", "a3", "a4"], limit=2)
        assert len(result) <= 2

    def test_edit_distance_near_miss(self):
        """A single-character typo should still produce a suggestion."""
        result = _fuzzy_tool_names("web_serch", ["web_search", "web_extract", "read_file"])
        assert "web_search" in result

    def test_no_match_when_too_far(self):
        """Completely unrelated names should not produce suggestions."""
        result = _fuzzy_tool_names("zzzzz", ["web_search", "read_file", "terminal"])
        assert result == []


# ── dispatch_tool_describe integration tests ────────────────────────────


def _make_tool_def(name: str, description: str = "test tool") -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {"type": "object", "properties": {}},
        },
    }


class TestDispatchToolDescribeFuzzy:
    """Integration: dispatch_tool_describe returns suggestions on miss."""

    @patch("tools.tool_search.is_deferrable_tool_name", return_value=True)
    def test_exact_match_returns_description(self, _mock):
        defs = [_make_tool_def("my_tool")]
        result = json.loads(
            dispatch_tool_describe(
                {"name": "my_tool"},
                current_tool_defs=defs,
                config=None,
            )
        )
        assert result.get("name") == "my_tool"
        assert "description" in result

    @patch("tools.tool_search.is_deferrable_tool_name", return_value=True)
    def test_typo_returns_suggestions(self, _mock):
        defs = [
            _make_tool_def("github_create_issue"),
            _make_tool_def("web_search"),
        ]
        result = json.loads(
            dispatch_tool_describe(
                {"name": "github_create"},  # substring of the real name
                current_tool_defs=defs,
                config=None,
            )
        )
        assert "error" in result
        assert "suggestions" in result
        assert "github_create_issue" in result["suggestions"]

    def test_non_deferrable_typo_returns_suggestions(self):
        """Even when the name is not deferrable, if a deferrable tool name
        is close, suggest it (#978 non-deferrable branch)."""
        defs = [_make_tool_def("mcp_search_web")]
        with patch(
            "tools.tool_search.is_deferrable_tool_name",
            side_effect=lambda name, config=None: name == "mcp_search_web",
        ):
            result = json.loads(
                dispatch_tool_describe(
                    {"name": "mcp_search"},  # close to "mcp_search_web"
                    current_tool_defs=defs,
                    config=None,
                )
            )
            assert "error" in result
            assert "suggestions" in result
            assert "mcp_search_web" in result["suggestions"]

    def test_completely_wrong_name_no_suggestions(self):
        result = json.loads(
            dispatch_tool_describe(
                {"name": "zzzzzzzzzz"},
                current_tool_defs=[_make_tool_def("web_search")],
                config=None,
            )
        )
        assert "error" in result
        # Should NOT have suggestions for a completely unrelated name.
        assert "suggestions" not in result

    def test_empty_name_returns_error(self):
        result = json.loads(
            dispatch_tool_describe(
                {"name": ""},
                current_tool_defs=[],
                config=None,
            )
        )
        assert "error" in result
        assert "required" in result["error"]