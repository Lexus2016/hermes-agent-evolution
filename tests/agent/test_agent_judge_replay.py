"""Tests for the Agent-as-Judge trace replay subsystem (issue #304)."""

import pytest

from agent.agent_judge import (
    DEFAULT_RUBRIC,
    replay_trace_steps,
    score_replayed_trace,
)


SUCCESS_TRACE = [
    {"role": "user", "content": "list the files"},
    {"role": "assistant", "content": "", "tool_calls": [{"function": {"name": "terminal"}, "id": "1"}]},
    {"role": "tool", "content": "a.py\nb.py"},
    {"role": "assistant", "content": "There are two files: a.py and b.py."},
]

FAILING_TRACE = [
    {"role": "user", "content": "run the build"},
    {"role": "assistant", "content": "", "tool_calls": [{"function": {"name": "terminal"}, "id": "1"}]},
    {"role": "tool", "content": "error: command not found"},
    {"role": "assistant", "content": "", "tool_calls": [{"function": {"name": "terminal"}, "id": "2"}]},
    {"role": "tool", "content": "error: command not found"},
    {"role": "assistant", "content": "", "tool_calls": [{"function": {"name": "terminal"}, "id": "3"}]},
    {"role": "tool", "content": "error: command not found"},
]


class TestReplayTraceSteps:
    def test_success_trace_steps(self):
        steps = list(replay_trace_steps(SUCCESS_TRACE))
        assert len(steps) == 2
        assert steps[0]["step"] == 1
        assert steps[0]["tool_calls"]
        assert len(steps[0]["tool_results"]) == 1
        assert steps[1]["has_final_answer"] is True
        assert steps[1]["tool_calls"] == []

    def test_failing_trace_steps(self):
        steps = list(replay_trace_steps(FAILING_TRACE))
        # Three assistant turns, each a terminal call.
        assert len(steps) == 3
        for step in steps:
            assert step["tool_calls"]
            assert len(step["tool_results"]) == 1

    def test_skips_non_dict_messages(self):
        mixed = [None, "garbage"] + SUCCESS_TRACE
        steps = list(replay_trace_steps(mixed))
        assert len(steps) == 2

    def test_empty_trace(self):
        assert list(replay_trace_steps([])) == []


class TestScoreReplayedTrace:
    def test_success_trace_scores(self):
        result = score_replayed_trace("s1", SUCCESS_TRACE, DEFAULT_RUBRIC)
        assert result["session_id"] == "s1"
        assert result["verdict"]["method"] == "heuristic"
        assert result["verdict"]["overall_score"] > 0.7
        assert len(result["steps"]) == 2
        assert result["steps"][0]["score"] == 1.0
        assert result["steps"][1]["score"] == 0.5

    def test_failing_trace_scores_low(self):
        result = score_replayed_trace("s2", FAILING_TRACE, DEFAULT_RUBRIC)
        assert result["verdict"]["overall_score"] < 0.5
        assert all(s["score"] == 0.0 for s in result["steps"])

    def test_empty_trace(self):
        result = score_replayed_trace("s3", [], DEFAULT_RUBRIC)
        assert result["verdict"]["overall_score"] < 0.5
        assert result["steps"] == []

    def test_dict_tool_result_content(self):
        trace = [
            {"role": "user", "content": "x"},
            {"role": "assistant", "content": "", "tool_calls": [{"function": {"name": "terminal"}, "id": "1"}]},
            {"role": "tool", "content": {"stdout": "ok"}},
            {"role": "assistant", "content": "Done."},
        ]
        steps = list(replay_trace_steps(trace))
        assert steps[0]["tool_results"][0]["content"] == {"stdout": "ok"}
        result = score_replayed_trace("s4", trace, DEFAULT_RUBRIC)
        assert result["steps"][0]["score"] == 1.0

    def test_serializable(self):
        import json

        result = score_replayed_trace("s5", SUCCESS_TRACE, DEFAULT_RUBRIC)
        # Should not raise.
        json.dumps(result)
