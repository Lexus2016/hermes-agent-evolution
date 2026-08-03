#!/usr/bin/env python3
"""Unit tests for the eval harness agent runner (issue #1515, Step 2)."""

from __future__ import annotations

import json
import sys
import os

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from eval_runner import EvalTrajectory, run_eval_task  # noqa: E402
from eval_tasks import EvalTask, load_task_set  # noqa: E402


class TestEvalTrajectory:
    def test_default_fields(self):
        traj = EvalTrajectory(task_id="t1")
        assert traj.tool_calls == []
        assert traj.model_turns == 0
        assert traj.error is None

    def test_to_dict(self):
        traj = EvalTrajectory(task_id="t1", final_answer="42", model_turns=1)
        d = traj.to_dict()
        assert d["task_id"] == "t1"
        assert d["final_answer"] == "42"
        assert d["model_turns"] == 1

    def test_to_jsonl_roundtrip(self):
        traj = EvalTrajectory(task_id="t1", final_answer="hello")
        parsed = json.loads(traj.to_jsonl())
        assert parsed["task_id"] == "t1"
        assert parsed["final_answer"] == "hello"


class TestRunEvalTask:
    def test_returns_trajectory(self):
        traj = run_eval_task(EvalTask(id="t1", prompt="What is 2+2?"))
        assert isinstance(traj, EvalTrajectory)
        assert traj.task_id == "t1"

    def test_captures_tool_calls(self):
        traj = run_eval_task(
            EvalTask(id="t2", prompt="List files", expected_tools=["search_files"]),
            agent_config={"tools": ["search_files"]},
        )
        assert len(traj.tool_calls) >= 1
        assert traj.tool_calls[0]["tool"] == "search_files"

    def test_captures_final_answer_and_turns(self):
        traj = run_eval_task(EvalTask(id="t3", prompt="Say hello"))
        assert traj.final_answer != ""
        assert traj.model_turns >= 1
        assert traj.duration_seconds > 0
        assert traj.error is None


class TestConfigPinning:
    def test_default_config(self):
        traj = run_eval_task(EvalTask(id="c1", prompt="test"))
        assert traj.agent_config["model"] == "test-pinned"
        assert "read_file" in traj.agent_config["tools"]

    def test_custom_config(self):
        traj = run_eval_task(
            EvalTask(id="c2", prompt="test"),
            agent_config={"model": "v2", "max_turns": 3},
        )
        assert traj.agent_config["model"] == "v2"
        assert traj.agent_config["max_turns"] == 3


class TestIntegration:
    def test_runs_against_real_tasks(self):
        tasks = load_task_set("1.0")
        for task in tasks[:2]:
            traj = run_eval_task(task)
            assert traj.task_id == task.id
            assert traj.error is None
