#!/usr/bin/env python3
"""Tests for the eval-harness runner and null-agent baseline (#1515, #1516).

The runner drives a real ``AIAgent`` in production; these tests substitute the
agent and the conversation driver at the seams ``run_eval_task`` exposes, so
the recorder wiring, trajectory assembly, error capture and scoring under test
are the same code a real run executes.
"""

from __future__ import annotations

import json
import os
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")
)

from eval_baseline import (  # noqa: E402
    NULL_AGENT_REFUSAL,
    _null_agent_factory,
    _null_run_conversation,
    run_null_agent_baseline,
    score_all,
    score_trajectory,
    summarize,
    write_scores,
)
from eval_runner import (  # noqa: E402
    EvalTrajectory,
    build_pinned_config,
    run_eval_task,
    run_task_set,
    write_trajectories,
)
from eval_tasks import EvalTask, latest_version, load_task_set  # noqa: E402


def _task(**kw) -> EvalTask:
    base = {
        "id": "t1",
        "prompt": "do the thing",
        "expected_tools": ["search_files"],
        "expected_result_pattern": "found-it",
        "category": "search",
        "difficulty": "easy",
    }
    base.update(kw)
    return EvalTask(**base)


def _fake_agent_factory(_pinned, on_start, on_complete):
    """An agent stand-in that exposes the recorder hooks it was handed."""
    return SimpleNamespace(on_start=on_start, on_complete=on_complete)


def _driver(*, tools=(), answer="", fail=False, raises=None):
    """Build a conversation driver that calls `tools` then returns `answer`."""

    def drive(agent, _prompt, **_kw):
        if raises is not None:
            raise raises
        for name in tools:
            agent.on_start(name, {"q": "x"})
            agent.on_complete(name, f"{name}-result")
        return {
            "response": answer,
            "conversation_history": [{"role": "assistant", "content": answer}],
            "failed": fail,
        }

    return drive


class TestPinnedConfig:
    def test_defaults_are_pinned(self):
        cfg = build_pinned_config()
        assert cfg["max_iterations"] > 0
        assert cfg["toolsets"]

    def test_overrides_apply(self):
        cfg = build_pinned_config({"model": "m1", "max_iterations": 3})
        assert cfg["model"] == "m1"
        assert cfg["max_iterations"] == 3

    def test_config_is_recorded_on_the_trajectory(self):
        """A trajectory is only comparable if it says what produced it."""
        traj = run_eval_task(
            _task(),
            {"model": "pinned-1", "max_iterations": 2},
            _fake_agent_factory,
            _driver(answer="x"),
        )
        assert traj.agent_config["model"] == "pinned-1"
        assert traj.agent_config["max_iterations"] == 2


class TestRunEvalTask:
    def test_captures_tool_calls_in_order(self):
        traj = run_eval_task(
            _task(),
            None,
            _fake_agent_factory,
            _driver(tools=["search_files", "read_file"], answer="found-it"),
        )
        assert traj.tools_used == ["search_files", "read_file"]
        assert traj.error is None

    def test_captures_tool_results(self):
        traj = run_eval_task(
            _task(), None, _fake_agent_factory, _driver(tools=["search_files"])
        )
        assert traj.tool_calls[0]["result"] == "search_files-result"

    def test_captures_final_answer_and_turns(self):
        traj = run_eval_task(
            _task(), None, _fake_agent_factory, _driver(answer="found-it here")
        )
        assert traj.final_answer == "found-it here"
        assert traj.model_turns == 1

    def test_exception_becomes_an_error_not_a_crash(self):
        """One task blowing up must not abort a whole baseline run."""
        traj = run_eval_task(
            _task(), None, _fake_agent_factory, _driver(raises=RuntimeError("boom"))
        )
        assert traj.error is not None
        assert "RuntimeError: boom" in traj.error
        assert traj.task_id == "t1"

    def test_agent_reported_failure_is_recorded(self):
        traj = run_eval_task(
            _task(), None, _fake_agent_factory, _driver(answer="x", fail=True)
        )
        assert traj.error is not None

    def test_duration_is_measured(self):
        traj = run_eval_task(_task(), None, _fake_agent_factory, _driver(answer="x"))
        assert traj.duration_seconds >= 0.0

    def test_run_task_set_returns_one_trajectory_per_task(self):
        tasks = [_task(id="a"), _task(id="b"), _task(id="c")]
        trajs = run_task_set(tasks, None, _fake_agent_factory, _driver(answer="x"))
        assert [t.task_id for t in trajs] == ["a", "b", "c"]


class TestTrajectorySerialization:
    def test_round_trips_through_jsonl(self):
        traj = EvalTrajectory(task_id="t1", final_answer="a", model_turns=2)
        parsed = json.loads(traj.to_jsonl())
        assert parsed["task_id"] == "t1"
        assert parsed["model_turns"] == 2

    def test_written_file_has_one_line_per_trajectory(self, tmp_path):
        trajs = [EvalTrajectory(task_id=f"t{i}") for i in range(3)]
        path = write_trajectories(trajs, str(tmp_path / "tr.jsonl"))
        assert len(open(path, encoding="utf-8").read().strip().splitlines()) == 3


class TestScoring:
    def test_full_credit_when_tools_and_pattern_match(self):
        traj = EvalTrajectory(
            task_id="t1",
            final_answer="found-it",
            tool_calls=[{"tool": "search_files", "args": {}, "result": "r"}],
        )
        assert score_trajectory(traj, _task())["total"] == 1.0

    def test_partial_credit_for_tools_without_the_pattern(self):
        traj = EvalTrajectory(
            task_id="t1",
            final_answer="nope",
            tool_calls=[{"tool": "search_files", "args": {}, "result": "r"}],
        )
        assert score_trajectory(traj, _task())["total"] == 0.5

    def test_error_gates_the_score_to_zero(self):
        """A run that errored scores 0 no matter what it did first."""
        traj = EvalTrajectory(
            task_id="t1",
            final_answer="found-it",
            tool_calls=[{"tool": "search_files", "args": {}, "result": "r"}],
            error="RuntimeError: boom",
        )
        assert score_trajectory(traj, _task())["total"] == 0.0

    def test_refusal_scores_zero(self):
        """The property the whole floor test rests on."""
        traj = EvalTrajectory(task_id="t1", final_answer=NULL_AGENT_REFUSAL)
        score = score_trajectory(traj, _task())
        assert score["total"] == 0.0
        assert score["tool_score"] == 0.0
        assert score["result_score"] == 0.0

    def test_clean_completion_alone_earns_nothing(self):
        """Regression for the defect the first null run exposed.

        ``completed`` used to average in as a third component, so an agent that
        refused every task still scored 0.33 — paying for the absence of a
        crash. It is a gate now, not a component.
        """
        traj = EvalTrajectory(task_id="t1", final_answer="", error=None)
        assert score_trajectory(traj, _task())["total"] == 0.0

    def test_task_without_expectations_cannot_inflate(self):
        traj = EvalTrajectory(task_id="t1", final_answer="anything")
        task = _task(expected_tools=[], expected_result_pattern=None)
        assert score_trajectory(traj, task)["total"] == 0.0

    def test_summary_counts_solved_and_errored(self):
        scores = [
            {"total": 1.0, "tool_score": 1.0, "error": None},
            {"total": 0.0, "tool_score": 0.0, "error": "boom"},
        ]
        summary = summarize(scores)
        assert summary["tasks"] == 2
        assert summary["solved"] == 1
        assert summary["errored"] == 1

    def test_summary_of_nothing_is_zero_not_a_crash(self):
        assert summarize([])["tasks"] == 0

    def test_scores_are_written_as_jsonl(self, tmp_path):
        path = write_scores([{"task_id": "t1", "total": 1.0}], str(tmp_path / "s.jsonl"))
        assert json.loads(open(path, encoding="utf-8").readline())["task_id"] == "t1"


class TestNullAgentBaseline:
    def test_null_agent_calls_no_tools_and_refuses(self):
        agent = _null_agent_factory({}, lambda *a, **k: None, lambda *a, **k: None)
        result = _null_run_conversation(agent, "anything")
        assert result["response"] == NULL_AGENT_REFUSAL
        assert result["failed"] is False

    def test_floor_over_the_real_task_set_is_zero(self, tmp_path):
        """The floor must be 0 on the shipped rubric.

        If this ever rises, the rubric has started rewarding something other
        than solving the task, and every improvement measured against it is
        suspect (#1267, BenchJack).
        """
        scores, summary = run_null_agent_baseline(
            scores_path=str(tmp_path / "scores-null.jsonl")
        )
        assert summary["tasks"] == len(load_task_set(latest_version()))
        assert summary["mean_total"] == 0.0
        assert summary["solved"] == 0
        assert all(s["total"] == 0.0 for s in scores)

    def test_baseline_writes_a_scores_file(self, tmp_path):
        out = tmp_path / "scores-null.jsonl"
        run_null_agent_baseline(scores_path=str(out))
        lines = out.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == len(load_task_set(latest_version()))
        assert json.loads(lines[0])["total"] == 0.0

    def test_baseline_can_emit_trajectories(self, tmp_path):
        run_null_agent_baseline(
            scores_path=str(tmp_path / "s.jsonl"),
            trajectories_path=str(tmp_path / "tr.jsonl"),
        )
        assert (tmp_path / "tr.jsonl").exists()

    def test_null_run_goes_through_the_real_trajectory_path(self, tmp_path):
        """The floor is only meaningful if it is measured on the real path."""
        scores, _ = run_null_agent_baseline(scores_path=str(tmp_path / "s.jsonl"))
        assert all("duration_seconds" in s for s in scores)
        assert all(s["error"] is None for s in scores)


class TestScoreAll:
    def test_matches_trajectories_to_their_tasks(self):
        tasks = [_task(id="a", expected_tools=["search_files"]), _task(id="b")]
        trajs = [
            EvalTrajectory(
                task_id="a",
                final_answer="found-it",
                tool_calls=[{"tool": "search_files", "args": {}, "result": "r"}],
            ),
            EvalTrajectory(task_id="b", final_answer="nothing"),
        ]
        scores = score_all(trajs, tasks)
        assert scores[0]["total"] == 1.0
        assert scores[1]["total"] == 0.0
