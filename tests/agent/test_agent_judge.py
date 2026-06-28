"""Tests for the Agent-as-a-Judge evaluation harness (issue #226)."""

from unittest.mock import MagicMock, patch

import pytest

from agent.agent_judge import (
    DEFAULT_RUBRIC,
    AgentJudge,
    JudgeVerdict,
    Rubric,
    RubricDimension,
    TraceSummary,
    _clamp,
    _extract_json_object,
    build_judge_messages,
    format_report_terminal,
    parse_judge_response,
    render_transcript_excerpt,
    score_trace_heuristic,
    score_trace_llm,
    summarize_trace,
)


def _llm_response(content: str, model: str = "test-judge-model") -> MagicMock:
    """Build a MagicMock shaped like an OpenAI chat completion response."""
    resp = MagicMock()
    resp.choices = [MagicMock()]
    resp.choices[0].message.content = content
    resp.model = model
    return resp


# A small, well-behaved trace: user asks, agent uses a tool, gets a result,
# delivers a plain-text final answer.
SUCCESS_TRACE = [
    {"role": "user", "content": "list the files"},
    {"role": "assistant", "content": "", "tool_calls": [{"function": {"name": "terminal"}, "id": "1"}]},
    {"role": "tool", "content": "a.py\nb.py"},
    {"role": "assistant", "content": "There are two files: a.py and b.py."},
]

# A failing trace: tool errors repeatedly, never delivers a final answer.
FAILING_TRACE = [
    {"role": "user", "content": "run the build"},
    {"role": "assistant", "content": "", "tool_calls": [{"function": {"name": "terminal"}, "id": "1"}]},
    {"role": "tool", "content": "error: command not found"},
    {"role": "assistant", "content": "", "tool_calls": [{"function": {"name": "terminal"}, "id": "2"}]},
    {"role": "tool", "content": "error: command not found"},
    {"role": "assistant", "content": "", "tool_calls": [{"function": {"name": "terminal"}, "id": "3"}]},
    {"role": "tool", "content": "error: command not found"},
]


class TestClamp:
    def test_in_range(self):
        assert _clamp(0.5) == 0.5

    def test_above_range(self):
        assert _clamp(1.7) == 1.0

    def test_below_range(self):
        assert _clamp(-2.0) == 0.0

    def test_non_numeric(self):
        assert _clamp("not a number") == 0.0

    def test_nan(self):
        assert _clamp(float("nan")) == 0.0


class TestRubric:
    def test_default_rubric_keys_unique(self):
        keys = DEFAULT_RUBRIC.keys()
        assert len(keys) == len(set(keys))
        assert "task_completion" in keys

    def test_empty_rubric_rejected(self):
        with pytest.raises(ValueError):
            Rubric(dimensions=())

    def test_duplicate_keys_rejected(self):
        with pytest.raises(ValueError):
            Rubric(dimensions=(
                RubricDimension("x", "X", "desc"),
                RubricDimension("x", "X2", "desc2"),
            ))

    def test_total_weight(self):
        # 2.0 + 1.5 + 1.0 + 1.0
        assert DEFAULT_RUBRIC.total_weight() == pytest.approx(5.5)

    def test_zero_weight_falls_back_to_count(self):
        r = Rubric(dimensions=(
            RubricDimension("a", "A", "d", weight=0.0),
            RubricDimension("b", "B", "d", weight=0.0),
        ))
        assert r.total_weight() == 2.0


class TestSummarizeTrace:
    def test_empty(self):
        s = summarize_trace([])
        assert s.message_count == 0
        assert s.has_final_answer is False
        assert s.tool_calls == 0

    def test_success_trace_signals(self):
        s = summarize_trace(SUCCESS_TRACE)
        assert s.user_turns == 1
        assert s.assistant_turns == 2
        assert s.tool_calls == 1
        assert s.tool_results == 1
        assert s.tool_failures == 0
        assert s.has_final_answer is True
        assert s.tool_names == ["terminal"]

    def test_failing_trace_signals(self):
        s = summarize_trace(FAILING_TRACE)
        assert s.tool_calls == 3
        assert s.tool_results == 3
        assert s.tool_failures == 3
        # Three consecutive identical tool calls -> one repetition spiral.
        assert s.repeated_tool_runs == 1
        assert s.has_final_answer is False

    def test_refusal_detected(self):
        trace = [
            {"role": "user", "content": "delete the database"},
            {"role": "assistant", "content": "I can't do that without confirmation."},
        ]
        s = summarize_trace(trace)
        assert s.refusals == 1
        # A refusal-only assistant turn IS a final-text answer, but completion
        # scoring handles that downstream.
        assert s.has_final_answer is True

    def test_ignores_non_dict_messages(self):
        s = summarize_trace([None, "garbage", {"role": "user", "content": "hi"}])
        assert s.user_turns == 1
        assert s.message_count == 3

    def test_to_dict_roundtrip(self):
        s = summarize_trace(SUCCESS_TRACE)
        d = s.to_dict()
        assert d["tool_calls"] == 1
        assert d["has_final_answer"] is True


class TestHeuristicScoring:
    def test_success_scores_high(self):
        v = score_trace_heuristic("s1", SUCCESS_TRACE)
        assert v.method == "heuristic"
        assert v.overall_score > 0.7
        assert v.passed()
        assert set(v.dimension_scores.keys()) == set(DEFAULT_RUBRIC.keys())

    def test_failure_scores_low(self):
        v = score_trace_heuristic("s2", FAILING_TRACE)
        assert v.method == "heuristic"
        assert v.overall_score < 0.5
        assert not v.passed()
        # Tool-use must be penalised: every tool result failed.
        assert v.dimension_scores["tool_use"] == 0.0

    def test_all_scores_in_range(self):
        for trace in (SUCCESS_TRACE, FAILING_TRACE, []):
            v = score_trace_heuristic("s", trace)
            for score in v.dimension_scores.values():
                assert 0.0 <= score <= 1.0
            assert 0.0 <= v.overall_score <= 1.0

    def test_custom_dimension_gets_neutral(self):
        rubric = Rubric(dimensions=(
            RubricDimension("task_completion", "Task", "d", weight=1.0),
            RubricDimension("novel_axis", "Novel", "d", weight=1.0),
        ))
        v = score_trace_heuristic("s", SUCCESS_TRACE, rubric)
        assert v.dimension_scores["novel_axis"] == 0.5


class TestJsonExtraction:
    def test_plain_object(self):
        assert _extract_json_object('{"a": 1}') == {"a": 1}

    def test_object_with_prose_around(self):
        text = 'Here is my verdict:\n{"scores": {"x": 0.5}}\nThanks!'
        assert _extract_json_object(text) == {"scores": {"x": 0.5}}

    def test_object_in_code_fence(self):
        text = '```json\n{"a": 2}\n```'
        assert _extract_json_object(text) == {"a": 2}

    def test_braces_inside_string_dont_break_parsing(self):
        text = '{"rationale": "it used a {placeholder} token"}'
        assert _extract_json_object(text) == {"rationale": "it used a {placeholder} token"}

    def test_first_object_with_trailing_garbage(self):
        # raw_decode must stop at the end of the first object and ignore the
        # trailing prose rather than choke on it.
        text = '{"scores": {"x": 0.5}} and then some closing remarks.'
        assert _extract_json_object(text) == {"scores": {"x": 0.5}}

    def test_skips_unparseable_leading_brace(self):
        # A bare "{" that isn't valid JSON, then the real object later.
        text = "note: { incomplete ... actually here: {\"a\": 1}"
        assert _extract_json_object(text) == {"a": 1}

    def test_no_json_returns_none(self):
        assert _extract_json_object("no json here at all") is None

    def test_empty_returns_none(self):
        assert _extract_json_object("") is None


class TestParseJudgeResponse:
    def test_valid_full_response(self):
        content = (
            '{"scores": {"task_completion": 0.9, "tool_use": 0.8, '
            '"reasoning": 0.7, "efficiency": 1.0}, "rationale": "Solid work."}'
        )
        parsed = parse_judge_response(content, DEFAULT_RUBRIC)
        assert parsed is not None
        assert parsed["scores"]["task_completion"] == 0.9
        assert parsed["rationale"] == "Solid work."

    def test_missing_dimension_rejected(self):
        # Missing "efficiency" -> whole response rejected.
        content = (
            '{"scores": {"task_completion": 0.9, "tool_use": 0.8, '
            '"reasoning": 0.7}}'
        )
        assert parse_judge_response(content, DEFAULT_RUBRIC) is None

    def test_out_of_range_scores_clamped(self):
        content = (
            '{"scores": {"task_completion": 1.9, "tool_use": -0.5, '
            '"reasoning": 0.5, "efficiency": 0.5}}'
        )
        parsed = parse_judge_response(content, DEFAULT_RUBRIC)
        assert parsed is not None
        assert parsed["scores"]["task_completion"] == 1.0
        assert parsed["scores"]["tool_use"] == 0.0

    def test_missing_scores_object_rejected(self):
        assert parse_judge_response('{"rationale": "hi"}', DEFAULT_RUBRIC) is None

    def test_garbage_rejected(self):
        assert parse_judge_response("the agent did great", DEFAULT_RUBRIC) is None

    def test_non_string_rationale_coerced(self):
        content = (
            '{"scores": {"task_completion": 0.5, "tool_use": 0.5, '
            '"reasoning": 0.5, "efficiency": 0.5}, "rationale": 123}'
        )
        parsed = parse_judge_response(content, DEFAULT_RUBRIC)
        assert parsed is not None
        assert parsed["rationale"] == ""


class TestBuildJudgeMessages:
    def test_includes_rubric_and_schema(self):
        summary = summarize_trace(SUCCESS_TRACE)
        msgs = build_judge_messages("s", summary, "excerpt", DEFAULT_RUBRIC, "do a thing")
        assert msgs[0]["role"] == "system"
        # Schema must name every rubric key.
        for key in DEFAULT_RUBRIC.keys():
            assert key in msgs[0]["content"]
        assert "do a thing" in msgs[1]["content"]
        assert "Objective trace signals" in msgs[1]["content"]

    def test_task_optional(self):
        summary = summarize_trace(SUCCESS_TRACE)
        msgs = build_judge_messages("s", summary, "excerpt", DEFAULT_RUBRIC, None)
        assert "Task the agent was given" not in msgs[1]["content"]


class TestRenderTranscriptExcerpt:
    def test_tool_calls_rendered(self):
        out = render_transcript_excerpt(SUCCESS_TRACE)
        assert "calls tools: terminal" in out
        assert "[tool result]" in out
        assert "[user]" in out

    def test_message_count_bounded(self):
        big = [{"role": "user", "content": f"m{i}"} for i in range(200)]
        out = render_transcript_excerpt(big, max_messages=20)
        assert "messages elided" in out

    def test_char_bound(self):
        big = [{"role": "user", "content": "x" * 1000} for _ in range(50)]
        out = render_transcript_excerpt(big, max_messages=100, max_chars=2000)
        assert "chars elided" in out
        assert len(out) < 3000


class TestScoreTraceLlm:
    def test_success_path(self):
        content = (
            '{"scores": {"task_completion": 1.0, "tool_use": 0.9, '
            '"reasoning": 0.8, "efficiency": 0.9}, "rationale": "Clean run."}'
        )
        with patch("agent.auxiliary_client.call_llm", return_value=_llm_response(content)):
            v = score_trace_llm("s1", SUCCESS_TRACE, task="list files")
        assert v is not None
        assert v.method == "llm"
        assert v.model == "test-judge-model"
        assert v.overall_score > 0.85
        assert v.rationale == "Clean run."

    def test_returns_none_on_invalid_schema(self):
        with patch("agent.auxiliary_client.call_llm", return_value=_llm_response("garbage")):
            assert score_trace_llm("s1", SUCCESS_TRACE) is None

    def test_returns_none_on_llm_exception(self):
        with patch("agent.auxiliary_client.call_llm", side_effect=RuntimeError("no provider")):
            assert score_trace_llm("s1", SUCCESS_TRACE) is None

    def test_returns_none_on_bad_response_shape(self):
        broken = MagicMock()
        broken.choices = []
        with patch("agent.auxiliary_client.call_llm", return_value=broken):
            assert score_trace_llm("s1", SUCCESS_TRACE) is None

    def test_passes_judge_task_name(self):
        content = (
            '{"scores": {"task_completion": 0.5, "tool_use": 0.5, '
            '"reasoning": 0.5, "efficiency": 0.5}, "rationale": "ok"}'
        )
        with patch("agent.auxiliary_client.call_llm", return_value=_llm_response(content)) as m:
            score_trace_llm("s1", SUCCESS_TRACE)
        assert m.call_args.kwargs["task"] == "agent_judge"
        assert m.call_args.kwargs["temperature"] == 0.0


class TestAgentJudgeEngine:
    def test_score_uses_llm_when_available(self):
        content = (
            '{"scores": {"task_completion": 1.0, "tool_use": 1.0, '
            '"reasoning": 1.0, "efficiency": 1.0}, "rationale": "Perfect."}'
        )
        judge = AgentJudge()
        with patch("agent.auxiliary_client.call_llm", return_value=_llm_response(content)):
            v = judge.score("s1", SUCCESS_TRACE, task="x")
        assert v.method == "llm"
        assert v.overall_score == 1.0

    def test_score_falls_back_to_heuristic_when_llm_fails(self):
        judge = AgentJudge()
        with patch("agent.auxiliary_client.call_llm", side_effect=RuntimeError("no provider")):
            v = judge.score("s1", SUCCESS_TRACE)
        assert v.method == "heuristic"
        assert v.overall_score > 0.7

    def test_use_llm_false_skips_llm(self):
        judge = AgentJudge()
        with patch("agent.auxiliary_client.call_llm") as m:
            v = judge.score("s1", SUCCESS_TRACE, use_llm=False)
        m.assert_not_called()
        assert v.method == "heuristic"


class TestVerdict:
    def test_to_dict_serializable(self):
        v = score_trace_heuristic("session-abc", SUCCESS_TRACE)
        d = v.to_dict()
        import json as _json

        # Must be JSON-serialisable for storage / dashboards.
        _json.dumps(d)
        assert d["session_id"] == "session-abc"
        assert "trace_summary" in d
        assert d["method"] == "heuristic"

    def test_passed_threshold(self):
        v = JudgeVerdict(
            session_id="s",
            overall_score=0.65,
            dimension_scores={},
            rationale="",
            method="heuristic",
            trace_summary=TraceSummary(),
        )
        assert not v.passed()
        assert not v.passed(0.7)
        assert v.passed(0.6)


class TestFormatReport:
    def test_contains_score_and_verdict(self):
        v = score_trace_heuristic("abc123", SUCCESS_TRACE)
        text = format_report_terminal(v)
        assert "Agent-as-a-Judge" in text
        assert "Overall" in text
        assert ("PASS" in text or "FAIL" in text)
        # Each dimension title shows up.
        assert "Task completion" in text

    def test_failing_verdict_shows_fail(self):
        v = score_trace_heuristic("abc", FAILING_TRACE)
        text = format_report_terminal(v)
        assert "FAIL" in text
