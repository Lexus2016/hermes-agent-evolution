"""Tests for the grader-driven subagent revision loop (issue #1871)."""

from unittest.mock import MagicMock, patch


def test_grader_in_schema():
    from tools.delegate_tool import DELEGATE_TASK_SCHEMA

    g = DELEGATE_TASK_SCHEMA["parameters"]["properties"]["grader"]
    assert g["type"] == "object"
    assert "rubric" in g["properties"]
    assert "min_score" in g["properties"]
    assert "max_revisions" in g["properties"]
    assert "rubric" in g.get("required", [])


def test_delegate_task_accepts_grader():
    from tools.delegate_tool import delegate_task

    result = delegate_task(goal="test", grader={"rubric": "r"})
    assert isinstance(result, str)  # error string (no parent), not TypeError


def _mock_grader_child(response_text):
    mock_child = MagicMock()
    mock_child.run_conversation.return_value = {"final_response": response_text}
    mock_child.session_id = "g-test"
    return mock_child


def test_grader_parse_pass():
    from tools.delegate_tool import _run_grader_subagent

    with patch(
        "tools.delegate_tool._build_child_preserving_parent_tools",
        return_value=_mock_grader_child(
            '{"score": 8.5, "verdict": "pass", "feedback": "ok"}'
        ),
    ):
        r = _run_grader_subagent("r", "s", "g", MagicMock())
    assert r["score"] == 8.5 and r["verdict"] == "pass"


def test_grader_parse_fail():
    from tools.delegate_tool import _run_grader_subagent

    with patch(
        "tools.delegate_tool._build_child_preserving_parent_tools",
        return_value=_mock_grader_child(
            '{"score": 3.0, "verdict": "fail", "feedback": "bad"}'
        ),
    ):
        r = _run_grader_subagent("r", "s", "g", MagicMock())
    assert r["verdict"] == "fail" and r["score"] == 3.0


def test_grader_unparseable_defaults_pass():
    from tools.delegate_tool import _run_grader_subagent

    with patch(
        "tools.delegate_tool._build_child_preserving_parent_tools",
        return_value=_mock_grader_child("no json here"),
    ):
        r = _run_grader_subagent("r", "s", "g", MagicMock())
    assert r["verdict"] == "pass" and r["score"] == 10.0


def test_grader_exception_defaults_pass():
    from tools.delegate_tool import _run_grader_subagent

    with patch(
        "tools.delegate_tool._build_child_preserving_parent_tools",
        side_effect=Exception("boom"),
    ):
        r = _run_grader_subagent("r", "s", "g", MagicMock())
    assert r["verdict"] == "pass"


def test_revisions_noop_without_grader():
    from tools.delegate_tool import _apply_grader_revisions

    results = [{"task_index": 0, "status": "completed", "summary": "ok"}]
    _apply_grader_revisions(results, [{"goal": "t"}], [], MagicMock(), None)
    assert "grader_score" not in results[0]


def test_revisions_noop_without_rubric():
    from tools.delegate_tool import _apply_grader_revisions

    results = [{"task_index": 0, "status": "completed", "summary": "ok"}]
    _apply_grader_revisions(results, [{"goal": "t"}], [], MagicMock(), {"min_score": 5})
    assert "grader_score" not in results[0]


def test_revisions_skip_errored():
    from tools.delegate_tool import _apply_grader_revisions

    results = [{"task_index": 0, "status": "error", "summary": None}]
    _apply_grader_revisions(results, [{"goal": "t"}], [], MagicMock(), {"rubric": "r"})
    assert "grader_score" not in results[0]


def test_revisions_pass_first_try():
    from tools.delegate_tool import _apply_grader_revisions

    results = [{"task_index": 0, "status": "completed", "summary": "good"}]
    mc = MagicMock()
    with patch(
        "tools.delegate_tool._run_grader_subagent",
        return_value={"score": 9.0, "verdict": "pass", "feedback": ""},
    ):
        _apply_grader_revisions(
            results,
            [{"goal": "t"}],
            [(0, {"goal": "t"}, mc)],
            MagicMock(),
            {"rubric": "r", "min_score": 7.0, "max_revisions": 1},
        )
    assert results[0]["grader_score"] == 9.0
    assert results[0]["grader_revisions"] == 0
    mc.run_conversation.assert_not_called()


def test_revisions_triggers_revision():
    from tools.delegate_tool import _apply_grader_revisions

    results = [{"task_index": 0, "status": "completed", "summary": "bad"}]
    mc = MagicMock()
    mc._last_final_response = "fixed"
    mc.session_id = "c1"
    grades = [
        {"score": 3.0, "verdict": "fail", "feedback": "vague"},
        {"score": 8.0, "verdict": "pass", "feedback": "good"},
    ]
    with patch("tools.delegate_tool._run_grader_subagent", side_effect=grades):
        _apply_grader_revisions(
            results,
            [{"goal": "build"}],
            [(0, {"goal": "build"}, mc)],
            MagicMock(),
            {"rubric": "r", "min_score": 7.0, "max_revisions": 2},
        )
    assert results[0]["grader_score"] == 8.0
    assert results[0]["grader_revisions"] == 1
    assert results[0]["summary"] == "fixed"
    assert mc.run_conversation.call_count == 1


def test_revisions_exhausts_budget():
    from tools.delegate_tool import _apply_grader_revisions

    results = [{"task_index": 0, "status": "completed", "summary": "bad"}]
    mc = MagicMock()
    mc._last_final_response = "still bad"
    mc.session_id = "c1"
    fail = {"score": 2.0, "verdict": "fail", "feedback": "wrong"}
    with patch("tools.delegate_tool._run_grader_subagent", return_value=fail):
        _apply_grader_revisions(
            results,
            [{"goal": "build"}],
            [(0, {"goal": "build"}, mc)],
            MagicMock(),
            {"rubric": "r", "min_score": 7.0, "max_revisions": 1},
        )
    assert results[0]["grader_score"] == 2.0
    assert "grader_feedback" in results[0]
    assert mc.run_conversation.call_count == 1
