#!/usr/bin/env python3
"""
Tests for bounded auto-retry on shallow delegations (issue #323).

When a delegated subagent returns narrative text WITHOUT executing any
tools, `_run_single_child` flags it as ``shallow_result``.  Historically
the parent was only ADVISED to re-delegate; the round-trip was already
wasted.  Issue #323 adds a bounded automatic re-delegation with an
escalated goal: on a shallow result the same child is re-run (budget 1,
hard ceiling 2) with a prefix that references the failure.  The retry
result replaces the shallow one when the retry actually calls tools.

Invariants under test:
  - shallow -> one retry -> success (tools called): retry result wins,
    shallow_result flag cleared, escalated goal handed to the child.
  - shallow -> retry STILL shallow: give up at budget, keep the
    shallow_result flag, never loop past the budget.
  - non-shallow first attempt: NO retry (run_conversation called once).
  - budget is never exceeded (run_conversation call count bounded).
  - budget=0 disables the retry entirely (back to advise-only behaviour).

Run with:  python -m pytest tests/tools/test_delegate_shallow_retry.py -q
"""

import json
import threading
import unittest
from unittest.mock import MagicMock, patch

from tools.delegate_tool import (
    _SHALLOW_RETRY_BUDGET_MAX,
    _get_shallow_retry_budget,
    delegate_task,
)


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
    return parent


def _shallow_result(text="I'll call web_search with query 'foo'..."):
    """A completed run with NO tool calls — the shallow failure mode."""
    return {
        "final_response": text,
        "completed": True,
        "interrupted": False,
        "api_calls": 1,
        "messages": [
            {"role": "user", "content": "do something"},
            {"role": "assistant", "content": text},
        ],
    }


def _tool_using_result(text="Found 3 results.", tool="web_search"):
    """A completed run that actually executed a tool."""
    return {
        "final_response": text,
        "completed": True,
        "interrupted": False,
        "api_calls": 2,
        "messages": [
            {"role": "user", "content": "do something"},
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": "tc_1",
                        "function": {
                            "name": tool,
                            "arguments": '{"query": "foo"}',
                        },
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "tc_1", "content": '{"results": [1,2,3]}'},
            {"role": "assistant", "content": text},
        ],
    }


class TestShallowRetryBudgetConfig(unittest.TestCase):
    def test_default_budget_is_one(self):
        with patch("tools.delegate_tool._load_config", return_value={}):
            self.assertEqual(_get_shallow_retry_budget(), 1)

    def test_budget_clamped_to_ceiling(self):
        with patch(
            "tools.delegate_tool._load_config",
            return_value={"shallow_retry_max": 99},
        ):
            self.assertEqual(_get_shallow_retry_budget(), _SHALLOW_RETRY_BUDGET_MAX)

    def test_budget_zero_disables(self):
        with patch(
            "tools.delegate_tool._load_config",
            return_value={"shallow_retry_max": 0},
        ):
            self.assertEqual(_get_shallow_retry_budget(), 0)

    def test_negative_clamped_to_zero(self):
        with patch(
            "tools.delegate_tool._load_config",
            return_value={"shallow_retry_max": -5},
        ):
            self.assertEqual(_get_shallow_retry_budget(), 0)

    def test_garbage_falls_back_to_default(self):
        with patch(
            "tools.delegate_tool._load_config",
            return_value={"shallow_retry_max": "not-a-number"},
        ):
            self.assertEqual(_get_shallow_retry_budget(), 1)

    def test_ceiling_is_two(self):
        self.assertEqual(_SHALLOW_RETRY_BUDGET_MAX, 2)


class TestShallowRetryBehaviour(unittest.TestCase):
    def test_shallow_then_retry_success(self):
        """shallow -> one retry that calls a tool -> retry result wins."""
        parent = _make_mock_parent(depth=0)
        with patch("run_agent.AIAgent") as MockAgent, patch(
            "tools.delegate_tool._get_shallow_retry_budget", return_value=1
        ):
            mock_child = MagicMock()
            mock_child.model = "claude-sonnet-4-6"
            mock_child.session_prompt_tokens = 0
            mock_child.session_completion_tokens = 0
            mock_child.run_conversation.side_effect = [
                _shallow_result(),
                _tool_using_result(text="Found 3 results."),
            ]
            MockAgent.return_value = mock_child

            result = json.loads(
                delegate_task(goal="Search the web for foo", parent_agent=parent)
            )
            entry = result["results"][0]

            # Retried exactly once (2 total runs).
            self.assertEqual(mock_child.run_conversation.call_count, 2)
            # Retry succeeded -> the shallow flag must be gone and the
            # winning summary is the tool-using one, not the narrative.
            self.assertNotEqual(entry.get("shallow_result"), True)
            self.assertIn("Found 3 results.", entry["summary"])
            self.assertTrue(len(entry["tool_trace"]) >= 1)
            self.assertEqual(entry["tool_trace"][0]["tool"], "web_search")
            # Bookkeeping: a retry happened.
            self.assertEqual(entry.get("shallow_retries"), 1)

    def test_escalated_goal_references_failure(self):
        """The retry goal must be escalated (reference the failure + demand a tool call)."""
        parent = _make_mock_parent(depth=0)
        seen_goals = []

        def _capture(*args, **kwargs):
            seen_goals.append(kwargs.get("user_message"))
            # First call shallow, second call tool-using.
            return _shallow_result() if len(seen_goals) == 1 else _tool_using_result()

        with patch("run_agent.AIAgent") as MockAgent, patch(
            "tools.delegate_tool._get_shallow_retry_budget", return_value=1
        ):
            mock_child = MagicMock()
            mock_child.session_prompt_tokens = 0
            mock_child.session_completion_tokens = 0
            mock_child.run_conversation.side_effect = _capture
            MockAgent.return_value = mock_child

            delegate_task(goal="Search the web for foo", parent_agent=parent)

            self.assertEqual(len(seen_goals), 2)
            first, second = seen_goals
            self.assertEqual(first, "Search the web for foo")
            # Escalated prompt must reference the no-tool failure and the
            # original goal, and must be different from the first goal.
            self.assertNotEqual(second, first)
            self.assertIn("Search the web for foo", second)
            lowered = second.lower()
            self.assertTrue("tool" in lowered)
            self.assertTrue(
                "no tool" in lowered
                or "did not" in lowered
                or "without" in lowered
                or "previous" in lowered
            )

    def test_shallow_then_still_shallow_gives_up_at_budget(self):
        """shallow -> retry still shallow -> stop at budget, keep the flag."""
        parent = _make_mock_parent(depth=0)
        with patch("run_agent.AIAgent") as MockAgent, patch(
            "tools.delegate_tool._get_shallow_retry_budget", return_value=1
        ):
            mock_child = MagicMock()
            mock_child.session_prompt_tokens = 0
            mock_child.session_completion_tokens = 0
            # Always shallow.
            mock_child.run_conversation.side_effect = [
                _shallow_result("narrative one"),
                _shallow_result("narrative two"),
            ]
            MockAgent.return_value = mock_child

            result = json.loads(
                delegate_task(goal="Search the web", parent_agent=parent)
            )
            entry = result["results"][0]

            # Budget 1 -> exactly one retry -> 2 total runs, never more.
            self.assertEqual(mock_child.run_conversation.call_count, 2)
            # Still shallow -> flag retained, parent still warned.
            self.assertEqual(entry.get("shallow_result"), True)
            self.assertIn("SHALLOW DELEGATION", entry["summary"])
            self.assertEqual(entry.get("shallow_retries"), 1)

    def test_non_shallow_first_attempt_no_retry(self):
        """A first attempt that calls tools must NOT trigger any retry."""
        parent = _make_mock_parent(depth=0)
        with patch("run_agent.AIAgent") as MockAgent, patch(
            "tools.delegate_tool._get_shallow_retry_budget", return_value=1
        ):
            mock_child = MagicMock()
            mock_child.session_prompt_tokens = 0
            mock_child.session_completion_tokens = 0
            mock_child.run_conversation.return_value = _tool_using_result()
            MockAgent.return_value = mock_child

            result = json.loads(
                delegate_task(goal="Search the web", parent_agent=parent)
            )
            entry = result["results"][0]

            # Exactly one run — no retry for a healthy delegation.
            self.assertEqual(mock_child.run_conversation.call_count, 1)
            self.assertNotEqual(entry.get("shallow_result"), True)
            self.assertNotIn("shallow_retries", entry)

    def test_budget_never_exceeded_ceiling(self):
        """Even with budget at the ceiling, the run count is bounded (1 + budget)."""
        parent = _make_mock_parent(depth=0)
        with patch("run_agent.AIAgent") as MockAgent, patch(
            "tools.delegate_tool._get_shallow_retry_budget",
            return_value=_SHALLOW_RETRY_BUDGET_MAX,
        ):
            mock_child = MagicMock()
            mock_child.session_prompt_tokens = 0
            mock_child.session_completion_tokens = 0
            # Always shallow — force the loop to exhaust the budget.
            mock_child.run_conversation.side_effect = [
                _shallow_result(f"narrative {i}") for i in range(10)
            ]
            MockAgent.return_value = mock_child

            result = json.loads(
                delegate_task(goal="Search the web", parent_agent=parent)
            )
            entry = result["results"][0]

            # 1 initial + at most _SHALLOW_RETRY_BUDGET_MAX retries.
            self.assertEqual(
                mock_child.run_conversation.call_count,
                1 + _SHALLOW_RETRY_BUDGET_MAX,
            )
            self.assertEqual(entry.get("shallow_result"), True)
            self.assertEqual(entry.get("shallow_retries"), _SHALLOW_RETRY_BUDGET_MAX)

    def test_budget_zero_disables_retry(self):
        """budget=0 -> advise-only legacy behaviour, no extra run."""
        parent = _make_mock_parent(depth=0)
        with patch("run_agent.AIAgent") as MockAgent, patch(
            "tools.delegate_tool._get_shallow_retry_budget", return_value=0
        ):
            mock_child = MagicMock()
            mock_child.session_prompt_tokens = 0
            mock_child.session_completion_tokens = 0
            mock_child.run_conversation.return_value = _shallow_result()
            MockAgent.return_value = mock_child

            result = json.loads(
                delegate_task(goal="Search the web", parent_agent=parent)
            )
            entry = result["results"][0]

            # No retry at all.
            self.assertEqual(mock_child.run_conversation.call_count, 1)
            self.assertEqual(entry.get("shallow_result"), True)
            self.assertIn("SHALLOW DELEGATION", entry["summary"])
            self.assertNotIn("shallow_retries", entry)

    def test_orchestrator_no_tool_calls_not_retried(self):
        """An orchestrator whose work is sub-delegation looks tool-less but is
        NOT shallow — re-running it would re-fire its child delegations. It
        must never be auto-retried (no double-execution of side-effecting work).
        """
        parent = _make_mock_parent(depth=0)
        # max_spawn_depth>=2 so role="orchestrator" survives _build_child_agent
        # (otherwise it degrades to leaf and _delegate_role is overwritten).
        with patch("run_agent.AIAgent") as MockAgent, patch(
            "tools.delegate_tool._get_shallow_retry_budget", return_value=2
        ), patch(
            "tools.delegate_tool._load_config",
            return_value={"max_spawn_depth": 2},
        ):
            mock_child = MagicMock()
            mock_child.model = "claude-sonnet-4-6"
            mock_child.session_prompt_tokens = 0
            mock_child.session_completion_tokens = 0
            mock_child.run_conversation.return_value = {
                "final_response": "orchestrated 2 workers",
                "completed": True,
                "interrupted": False,
                "api_calls": 1,
                "messages": [],
            }
            MockAgent.return_value = mock_child

            result = json.loads(
                delegate_task(
                    goal="Coordinate two research streams",
                    role="orchestrator",
                    parent_agent=parent,
                )
            )
            entry = result["results"][0]

            # Ran exactly once — no retry despite the empty tool_trace.
            self.assertEqual(mock_child.run_conversation.call_count, 1)
            self.assertNotIn("shallow_retries", entry)

    def test_retry_recovers_after_first_retry_still_shallow(self):
        """budget 2: shallow -> shallow -> tool-using succeeds; flag cleared."""
        parent = _make_mock_parent(depth=0)
        with patch("run_agent.AIAgent") as MockAgent, patch(
            "tools.delegate_tool._get_shallow_retry_budget",
            return_value=2,
        ):
            mock_child = MagicMock()
            mock_child.session_prompt_tokens = 0
            mock_child.session_completion_tokens = 0
            mock_child.run_conversation.side_effect = [
                _shallow_result("narrative one"),
                _shallow_result("narrative two"),
                _tool_using_result(text="Recovered on second retry."),
            ]
            MockAgent.return_value = mock_child

            result = json.loads(
                delegate_task(goal="Search the web", parent_agent=parent)
            )
            entry = result["results"][0]

            self.assertEqual(mock_child.run_conversation.call_count, 3)
            self.assertNotEqual(entry.get("shallow_result"), True)
            self.assertIn("Recovered on second retry.", entry["summary"])
            self.assertEqual(entry.get("shallow_retries"), 2)


if __name__ == "__main__":
    unittest.main()
