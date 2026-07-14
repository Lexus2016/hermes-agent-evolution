"""Tests for search_files fail threshold override (#973).

The loop-guard should trip for ``search_files`` failures at a lower
threshold (3) than the generic idempotent default (4), so the agent
gets the repo_map diversion hint sooner when search_files keeps
failing with regex/glob parse errors.
"""

import json

from agent.loop_guard import maybe_nudge


def _assistant_tool_call(tool_name: str, args: dict | None = None) -> dict:
    return {
        "role": "assistant",
        "tool_calls": [
            {
                "id": f"call_{tool_name}",
                "type": "function",
                "function": {
                    "name": tool_name,
                    "arguments": json.dumps(args or {}),
                },
            }
        ],
    }


def _tool_result(content: str) -> dict:
    return {"role": "tool", "tool_call_id": "call_search_files", "content": content}


def _search_files_fail_run(n: int) -> list[dict]:
    """Build a message list of ``n`` consecutive failed search_files calls."""
    msgs: list[dict] = []
    for i in range(n):
        msgs.append(
            _assistant_tool_call(
                "search_files",
                {"pattern": f"broken_regex_{i}", "target": "content"},
            )
        )
        msgs.append(
            _tool_result(
                "Error: regex parse error at position 0: unexpected metacharacter"
            )
        )
    return msgs


class TestSearchFilesFailThreshold:
    """search_files should trip at 3 consecutive failures (#973)."""

    def test_trips_at_3_failures(self):
        """At 3 failures, the nudge should fire (was 4 before #973)."""
        msgs = _search_files_fail_run(3)
        nudge = maybe_nudge(msgs)
        assert nudge is not None
        assert "search_files" in nudge

    def test_no_trip_at_2_failures(self):
        """At 2 failures, the nudge should NOT fire yet."""
        msgs = _search_files_fail_run(2)
        nudge = maybe_nudge(msgs)
        assert nudge is None

    def test_nudge_includes_repo_map_hint(self):
        """The nudge should include the repo_map diversion hint."""
        msgs = _search_files_fail_run(4)
        nudge = maybe_nudge(msgs)
        assert nudge is not None
        assert "repo_map" in nudge

    def test_nudge_includes_regex_error_advice(self):
        """The nudge should include advice about regex/glob parse errors."""
        msgs = _search_files_fail_run(4)
        nudge = maybe_nudge(msgs)
        assert nudge is not None
        # The #973 hint mentions glob patterns or parse errors.
        assert "glob" in nudge.lower() or "regex" in nudge.lower()