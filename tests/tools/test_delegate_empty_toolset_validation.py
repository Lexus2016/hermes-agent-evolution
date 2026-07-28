#!/usr/bin/env python3
"""
Tests for empty-toolset validation in delegated sub-agents (issue #1387).

When a sub-agent's toolsets resolve to zero tools, delegate_task appends a
structured error entry to results (status='error') and skips launching that
child — preserving the {'results': [...]} contract without raising.

Run with:  python -m pytest tests/tools/test_delegate_empty_toolset_validation.py -q
"""

import json
import threading
import unittest
from unittest.mock import MagicMock, patch

from tools.delegate_tool import delegate_task


def _make_mock_parent(depth=0):
    parent = MagicMock()
    parent.base_url = "https://openrouter.ai/api/v1"
    parent.api_key = "***"
    parent.provider = "openrouter"
    parent.api_mode = "chat_completions"
    parent.model = "anthropic/claude-sonnet-4"
    parent.platform = "cli"
    parent.providers_allowed = None
    parent.providers_ignored = None
    parent.providers_order = None
    parent.provider_sort = None
    parent._session_db = None
    parent._delegate_depth = depth
    parent._active_children = []
    parent._active_children_lock = threading.Lock()
    parent._print_fn = None
    parent.tool_progress_callback = None
    parent.thinking_callback = None
    parent.enabled_toolsets = ["terminal", "file", "web"]
    parent.valid_tool_names = {"terminal", "file", "web_search"}
    return parent


def _make_mock_child(empty=False):
    """Build a mock child agent.  When empty=True, simulates 0 tools."""
    child = MagicMock()
    child.valid_tool_names = set() if empty else {"web_search", "read_file"}
    child._delegate_resolved_toolsets = [] if empty else ["web", "file"]
    child._delegate_requested_toolsets = []
    child._delegate_denied_toolsets = []
    child._delegate_role = "leaf"
    child._delegate_depth = 1
    child._subagent_id = "sa-test"
    child._parent_subagent_id = None
    child._parent_turn_id = ""
    child.session_id = "test-session"
    child.session_prompt_tokens = 0
    child.session_completion_tokens = 0
    child._credential_pool = None
    child._print_fn = None
    child.tool_progress_callback = None
    return child


class TestEmptyToolsetValidation(unittest.TestCase):
    """Sub-agents that resolve to 0 tools must be skipped, not launched."""

    def test_empty_toolset_produces_error_result(self):
        """0 tools → error entry in results, child never run, {'results': [...]}."""
        parent = _make_mock_parent()
        with patch("tools.delegate_tool._build_child_agent") as MockBuild:
            child = _make_mock_child(empty=True)
            MockBuild.return_value = child
            result = json.loads(delegate_task(goal="foo", parent_agent=parent))

        self.assertIn("results", result)
        entry = result["results"][0]
        self.assertEqual(entry["status"], "error")
        self.assertIn("toolset validation failed", entry["error"])
        child.run_conversation.assert_not_called()

    def test_non_empty_toolset_runs_normally(self):
        """≥1 tool → normal flow, no error entry."""
        parent = _make_mock_parent()
        with patch("tools.delegate_tool._build_child_agent") as MockBuild:
            child = _make_mock_child(empty=False)
            child.run_conversation.return_value = {
                "final_response": "ok",
                "completed": True,
                "api_calls": 1,
                "messages": [
                    {"role": "user", "content": "foo"},
                    {
                        "role": "assistant",
                        "tool_calls": [
                            {
                                "id": "tc1",
                                "function": {"name": "web_search", "arguments": "{}"},
                            }
                        ],
                    },
                    {"role": "tool", "tool_call_id": "tc1", "content": "{}"},
                    {"role": "assistant", "content": "ok"},
                ],
            }
            MockBuild.return_value = child
            result = json.loads(delegate_task(goal="foo", parent_agent=parent))

        self.assertIn("results", result)
        self.assertNotEqual(result["results"][0]["status"], "error")
        child.run_conversation.assert_called_once()

    def test_batch_all_empty_returns_error_per_task(self):
        """All tasks empty → one error per task, no IndexError."""
        parent = _make_mock_parent()
        with patch("tools.delegate_tool._build_child_agent") as MockBuild:
            MockBuild.return_value = _make_mock_child(empty=True)
            tasks = [{"goal": "A", "context": None}, {"goal": "B", "context": None}]
            result = json.loads(delegate_task(tasks=tasks, parent_agent=parent))

        self.assertEqual(len(result["results"]), 2)
        for i, entry in enumerate(result["results"]):
            self.assertEqual(entry["status"], "error")
            self.assertEqual(entry["task_index"], i)

    def test_batch_mixed_empty_and_valid(self):
        """Some empty, some valid → empty skipped, valid run."""
        parent = _make_mock_parent()
        empty = _make_mock_child(empty=True)
        valid = _make_mock_child(empty=False)
        valid.run_conversation.return_value = {
            "final_response": "ok",
            "completed": True,
            "api_calls": 1,
            "messages": [
                {"role": "user", "content": "Valid"},
                {
                    "role": "assistant",
                    "tool_calls": [
                        {
                            "id": "tc1",
                            "function": {"name": "web_search", "arguments": "{}"},
                        }
                    ],
                },
                {"role": "tool", "tool_call_id": "tc1", "content": "{}"},
                {"role": "assistant", "content": "ok"},
            ],
        }
        with patch("tools.delegate_tool._build_child_agent") as MockBuild:
            MockBuild.side_effect = [empty, valid]
            tasks = [
                {"goal": "Empty", "context": None},
                {"goal": "Valid", "context": None},
            ]
            result = json.loads(delegate_task(tasks=tasks, parent_agent=parent))

        entries = {e["task_index"]: e for e in result["results"]}
        self.assertEqual(entries[0]["status"], "error")
        self.assertNotEqual(entries[1]["status"], "error")
        empty.run_conversation.assert_not_called()
        valid.run_conversation.assert_called_once()

    def test_error_entry_has_required_fields(self):
        """Error entry must carry fields the parent expects in any result."""
        parent = _make_mock_parent()
        with patch("tools.delegate_tool._build_child_agent") as MockBuild:
            MockBuild.return_value = _make_mock_child(empty=True)
            result = json.loads(delegate_task(goal="foo", parent_agent=parent))

        entry = result["results"][0]
        for field in (
            "task_index",
            "status",
            "summary",
            "error",
            "exit_reason",
            "api_calls",
            "duration_seconds",
            "_child_role",
        ):
            self.assertIn(field, entry)
        self.assertEqual(entry["api_calls"], 0)
        self.assertIsNone(entry["summary"])


if __name__ == "__main__":
    unittest.main()
