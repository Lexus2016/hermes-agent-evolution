"""Tests for tool_describe on directly-available (non-deferrable) tools (#107).

Before #107, calling ``tool_describe`` on a tool whose def is already in the
active toolset — a core tool like ``terminal``, or an ``mcp__*`` tool whose
registry entry is transiently missing at dispatch time — returned a
"not a deferrable tool" error even though the schema was sitting in
``current_tool_defs``. The error wasted a round-trip and could mislead the
model into concluding the tool was unavailable (24 errors across 19 sessions
in 14d; affected lookups: terminal, exec, bash, exec_command,
mcp__tqmemory__semantic_search). tool_describe now returns the schema for any
name present in the active toolset; fuzzy suggestions (#978) and the error
paths are unchanged for names that are genuinely absent, and tool_call's
non-deferrable guard is untouched (describe-side fix only).
"""

import json
from unittest.mock import patch

from tools.tool_search import (
    ToolSearchConfig,
    clear_describe_cache,
    dispatch_tool_describe,
    resolve_underlying_call,
)


def _td(name, description="test tool", properties=None):
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {"type": "object", "properties": properties or {}},
        },
    }


class TestDescribeDirectTool:
    """tool_describe returns the schema for tools already in the toolset."""

    def test_direct_core_tool_returns_schema(self):
        result = json.loads(
            dispatch_tool_describe(
                {"name": "terminal"},
                current_tool_defs=[_td("terminal", "Run shell")],
            )
        )
        assert "error" not in result
        # Same payload shape as a successful deferred describe, so
        # downstream consumers cannot tell the two paths apart.
        assert set(result) == {"name", "description", "parameters"}
        assert result["name"] == "terminal"
        assert result["description"] == "Run shell"
        assert result["parameters"] == {"type": "object", "properties": {}}

    def test_direct_tool_parameters_preserved(self):
        props = {"path": {"type": "string"}}
        result = json.loads(
            dispatch_tool_describe(
                {"name": "read_file"},
                current_tool_defs=[_td("read_file", "Read a file", props)],
            )
        )
        assert "error" not in result
        assert result["parameters"]["properties"] == props

    def test_direct_success_has_no_error_fields(self):
        """A successful describe must NOT carry error-only fields."""
        result = json.loads(
            dispatch_tool_describe(
                {"name": "terminal"},
                current_tool_defs=[_td("terminal", "Run shell")],
            )
        )
        assert "error" not in result
        assert "reason" not in result
        assert "recovery" not in result
        assert "suggestions" not in result

    def test_near_miss_does_not_return_schema(self):
        """Exact-match only: a typo of a core tool must NOT get the schema —
        it falls through to the standard non-deferrable error. Core tools
        are not in the deferred catalog, so there is nothing to suggest."""
        result = json.loads(
            dispatch_tool_describe(
                {"name": "termianl"},
                current_tool_defs=[_td("terminal", "Run shell")],
            )
        )
        assert "error" in result
        assert result.get("reason") == "not_deferrable"
        assert "description" not in result

    def test_fuzzy_suggestions_still_work_for_deferrable_catalog(self):
        """#978 regression: a typo that is close to a DEFERRABLE tool still
        gets suggestions (the #107 lookup is exact-match only)."""
        defs = [_td("mcp_search_web")]
        with patch(
            "tools.tool_search.is_deferrable_tool_name",
            side_effect=lambda name, *a, **kw: name == "mcp_search_web",
        ):
            result = json.loads(
                dispatch_tool_describe(
                    {"name": "mcp_search"},
                    current_tool_defs=defs,
                )
            )
        assert "error" in result
        assert "suggestions" in result
        assert "mcp_search_web" in result["suggestions"]

    def test_unknown_name_still_errors(self):
        result = json.loads(
            dispatch_tool_describe(
                {"name": "zzzz_not_a_tool"},
                current_tool_defs=[_td("terminal", "Run shell")],
            )
        )
        assert "error" in result
        assert "suggestions" not in result
        assert result.get("reason") == "not_deferrable"

    def test_empty_defs_direct_lookup_errors(self):
        """No crash when the toolset is empty and the name is a known core
        tool: falls through to the standard error path."""
        result = json.loads(
            dispatch_tool_describe(
                {"name": "terminal"},
                current_tool_defs=[],
            )
        )
        assert "error" in result

    def test_transiently_unregistered_mcp_tool_in_defs_returns_schema(self):
        """An mcp__ tool granted to the session but not resolvable in the
        registry at dispatch time (is_deferrable_tool_name -> False) still
        describes from the def list — the session truth. Matches the #107
        evidence row for mcp__tqmemory__semantic_search."""
        defs = [_td("mcp__tqmemory__semantic_search", "Semantic search")]
        with patch(
            "tools.tool_search.is_deferrable_tool_name",
            return_value=False,
        ):
            result = json.loads(
                dispatch_tool_describe(
                    {"name": "mcp__tqmemory__semantic_search"},
                    current_tool_defs=defs,
                )
            )
        assert "error" not in result
        assert result["description"] == "Semantic search"

    def test_direct_result_is_cacheable(self):
        """Successful direct describes enter the #1015 cache like deferred
        ones (identity, not just equality)."""
        try:
            defs = [_td("terminal", "Run shell")]
            r1 = dispatch_tool_describe(
                {"name": "terminal"}, current_tool_defs=defs
            )
            r2 = dispatch_tool_describe(
                {"name": "terminal"}, current_tool_defs=defs
            )
            assert r1 == r2
            assert "terminal" in r1
        finally:
            clear_describe_cache()

    def test_tool_call_boundary_unchanged(self):
        """#107 is describe-side only: tool_call must still refuse a
        directly-available core tool — the bridge is not a backdoor into
        the visible toolset."""
        cfg = ToolSearchConfig.from_raw({"enabled": "on"})
        _name, _args, err = resolve_underlying_call(
            {"name": "terminal", "arguments": {}}, cfg
        )
        assert err is not None
        assert "not a deferrable" in err
