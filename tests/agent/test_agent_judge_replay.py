"""Tests for the Agent-as-Judge trace replay subsystem (issue #304)."""

from agent.agent_judge import (
    AgentJudge,
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


class TestMultimodalAndRobustness:
    def test_multimodal_content_blocks_are_coerced(self):
        trace = [
            {
                "role": "assistant",
                "content": [{"type": "text", "text": "thinking"}],
                "tool_calls": [{"function": {"name": "terminal"}, "id": "1"}],
            },
            {"role": "tool", "content": "ok"},
            {"role": "assistant", "content": [{"type": "text", "text": "Done."}]},
        ]
        steps = list(replay_trace_steps(trace))
        assert len(steps) == 2
        assert steps[0]["content"] == "thinking"
        assert steps[1]["content"] == "Done."
        assert steps[1]["has_final_answer"] is True

    def test_non_str_content_does_not_crash(self):
        # The #307 regression: bare `.strip()` on a dict/list content raised.
        trace = [
            {
                "role": "assistant",
                "content": {"weird": "dict"},
                "tool_calls": [{"function": {"name": "terminal"}, "id": "1"}],
            },
            {"role": "tool", "content": "ok"},
        ]
        steps = list(replay_trace_steps(trace))
        assert len(steps) == 1
        assert steps[0]["content"] == ""

    def test_assistant_with_dict_content_and_no_tools_is_skipped(self):
        assert list(replay_trace_steps([{"role": "assistant", "content": {"x": 1}}])) == []


class TestAgentJudgeScoreReplay:
    def test_engine_method_matches_module_function(self):
        judge = AgentJudge(DEFAULT_RUBRIC)
        assert judge.score_replay("s", SUCCESS_TRACE) == score_replayed_trace(
            "s", SUCCESS_TRACE, DEFAULT_RUBRIC
        )

    def test_engine_method_step_scores(self):
        result = AgentJudge(DEFAULT_RUBRIC).score_replay("s", SUCCESS_TRACE)
        assert result["session_id"] == "s"
        assert result["steps"][0]["score"] == 1.0
        assert result["steps"][1]["score"] == 0.5
