"""Tests for delegate_tool pre-dispatch tool validation (#1387).

Verifies that when a sub-agent's resolved toolset is empty (all requested
toolsets were unrecognized or stripped), the delegation fails fast with a
clear error instead of launching a toolless agent that spirals.
"""

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from tools.delegate_tool import delegate_task


def _make_parent():
    """Create a minimal mock parent agent."""
    return SimpleNamespace(
        enabled_toolsets=["terminal", "file"],
        valid_tool_names=["terminal", "read_file", "write_file", "patch"],
        model="test-model",
        provider=None,
        base_url=None,
        api_key=None,
        api_mode=None,
        acp_command=None,
        acp_args=[],
        reasoning_config=None,
        _fallback_chain=None,
        providers_allowed=None,
        providers_ignored=None,
        providers_order=None,
        provider_sort=None,
        provider_require_parameters=False,
        provider_data_collection=None,
        request_overrides={},
        session_id="test-session",
        _session_db=None,
        _delegate_depth=0,
        _print_fn=None,
        _active_children=[],
        prefill_messages=None,
    )


class TestEmptyToolsetValidation:
    """A sub-agent with 0 resolved tools must fail fast (#1387)."""

    def test_aborts_when_no_tools_resolved(self):
        """If toolset resolution yields 0 tools, delegation returns an error."""
        parent = _make_parent()
        # Toolsets is not specified (None) — children inherit parent's tools.
        # We mock _build_child_agent to return a child with NO valid tools.
        mock_child = MagicMock()
        mock_child.valid_tool_names = []
        mock_child._denied_toolsets_for_prompt = []

        with patch("tools.delegate_tool._build_child_agent", return_value=mock_child):
            with patch(
                "tools.delegation_live_log.create_live_transcripts",
                return_value=("id", [], []),
            ):
                with patch(
                    "tools.delegate_tool._resolve_delegation_credentials"
                ):
                    result = delegate_task(
                        goal="test goal",
                        parent_agent=parent,
                    )

        # Should be an error, not a launched delegation
        result_data = json.loads(result) if isinstance(result, str) else result
        assert "error" in result_data or (
            isinstance(result_data, dict) and "error" in str(result_data)
        )

    def test_does_not_abort_when_tools_present(self):
        """If toolset resolution yields >=1 tool, delegation proceeds normally."""
        parent = _make_parent()
        mock_child = MagicMock()
        mock_child.valid_tool_names = ["terminal", "read_file"]
        mock_child._denied_toolsets_for_prompt = []

        with patch("tools.delegate_tool._build_child_agent", return_value=mock_child):
            with patch(
                "tools.delegation_live_log.create_live_transcripts",
                return_value=("id", [], []),
            ):
                with patch(
                    "tools.delegate_tool._resolve_delegation_credentials"
                ):
                    with patch(
                        "tools.delegate_tool._run_single_child",
                        return_value={"task": 0, "summary": "ok"},
                    ):
                        result = delegate_task(
                            goal="test goal",
                            parent_agent=parent,
                        )

        # Should NOT contain an abort error
        result_str = result if isinstance(result, str) else json.dumps(result)
        assert "resolved to 0 tools" not in result_str
