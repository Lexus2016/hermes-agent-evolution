#!/usr/bin/env python3
"""Unit tests for the eval harness agent runner (issue #1515, Step 2)."""

from __future__ import annotations

import json
import sys
import os

import pytest

# Ensure scripts/ is on the path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from eval_runner import EvalTrajectory, run_eval_task  # noqa: E402
from eval_tasks import EvalTask, load_task_set  # noqa: E402


# ---------------------------------------------------------------------------
# EvalTrajectory dataclass
# ---------------------------------------------------------------------------


class TestEvalTrajectoryDataclass:
    def test_default_fields(self):
        traj = EvalTrajectory(task_id="test-1")
        assert traj.task_id == "test-1"
        assert traj.tool_calls == []
        assert traj.model_turns == 0
        assert traj.final_answer == ""
        assert traj.duration_seconds == 0.0
        assert traj.error is None
        assert traj.agent_config == {}

    def test_to_dict(self):
        traj = EvalTrajectory(
            task_id="t1",
            tool_calls=[{"tool": "read_file", "args": {}, "result": "ok", "timestamp": 1.0}],
            model_turns=2,
            final_answer="42",
            duration_seconds=1.5,
            agent_config={"model": "test"},
        )
        d = traj.to_dict()
        assert d["task_id"] == "t1"
        assert len(d["tool_calls"]) == 1
        assert d["model_turns"] == 2
        assert d["final_answer"] == "42"
        assert d["duration_seconds"] == 1.5
        assert d["error"] is None
        assert d["agent_config"]["model"] == "test"

    def test_to_jsonl(self):
        traj = EvalTrajectory(task_id="t1", final_answer="hello")
        line = traj.to_jsonl()
        assert line.endswith("\n")
        parsed = json.loads(line)
        assert parsed["task_id"] == "t1"
        assert parsed["final_answer"] == "hello"


# ---------------------------------------------------------------------------
# run_eval_task — basic execution
# ---------------------------------------------------------------------------


class TestRunEvalTask:
    def test_returns_trajectory_with_task_id(self):
        task = EvalTask(id="test-1", prompt="What is 2+2?")
        traj = run_eval_task(task)
        assert isinstance(traj, EvalTrajectory)
        assert traj.task_id == "test-1"

    def test_captures_tool_calls(self):
        task = EvalTask(id="test-2", prompt="List files", expected_tools=["search_files"])
        traj = run_eval_task(task, agent_config={"tools": ["search_files"]})
        assert len(traj.tool_calls) >= 1
        assert traj.tool_calls[0]["tool"] == "search_files"

    def test_captures_final_answer(self):
        task = EvalTask(id="test-3", prompt="Say hello")
        traj = run_eval_task(task)
        assert traj.final_answer != ""

    def test_records_model_turns(self):
        task = EvalTask(id="test-4", prompt="Read a file")
        traj = run_eval_task(task)
        assert traj.model_turns >= 1

    def test_duration_is_positive(self):
        task = EvalTask(id="test-5", prompt="Quick task")
        traj = run_eval_task(task)
        assert traj.duration_seconds > 0

    def test_no_error_on_success(self):
        task = EvalTask(id="test-6", prompt="Easy task")
        traj = run_eval_task(task)
        assert traj.error is None


# ---------------------------------------------------------------------------
# Agent config pinning
# ---------------------------------------------------------------------------


class TestAgentConfigPinning:
    def test_default_config(self):
        task = EvalTask(id="test-cfg-1", prompt="test")
        traj = run_eval_task(task)
        assert traj.agent_config["model"] == "test-pinned"
        assert "read_file" in traj.agent_config["tools"]
        assert traj.agent_config["max_turns"] == 5

    def test_custom_config(self):
        task = EvalTask(id="test-cfg-2", prompt="test")
        custom = {
            "model": "custom-model",
            "tools": ["search_files"],
            "system_prompt": "Custom prompt",
            "max_turns": 3,
        }
        traj = run_eval_task(task, agent_config=custom)
        assert traj.agent_config["model"] == "custom-model"
        assert traj.agent_config["tools"] == ["search_files"]
        assert traj.agent_config["system_prompt"] == "Custom prompt"
        assert traj.agent_config["max_turns"] == 3

    def test_config_recorded_in_trajectory(self):
        task = EvalTask(id="test-cfg-3", prompt="test")
        traj = run_eval_task(task, agent_config={"model": "pinned-v2"})
        assert "model" in traj.agent_config
        assert traj.agent_config["model"] == "pinned-v2"


# ---------------------------------------------------------------------------
# Trajectory serialization
# ---------------------------------------------------------------------------


class TestTrajectorySerialization:
    def test_full_trajectory_serializes(self):
        task = EvalTask(id="test-ser-1", prompt="test prompt for serialization")
        traj = run_eval_task(task)
        d = traj.to_dict()
        # Re-serialize to JSON and back
        raw = json.dumps(d)
        parsed = json.loads(raw)
        assert parsed["task_id"] == "test-ser-1"

    def test_jsonl_line_is_valid_json(self):
        task = EvalTask(id="test-ser-2", prompt="test")
        traj = run_eval_task(task)
        line = traj.to_jsonl().strip()
        parsed = json.loads(line)
        assert parsed["task_id"] == "test-ser-2"


# ---------------------------------------------------------------------------
# Integration with real task set
# ---------------------------------------------------------------------------


class TestIntegrationWithTaskSet:
    def test_runs_against_real_tasks(self):
        tasks = load_task_set("1.0")
        assert len(tasks) >= 3
        for task in tasks[:2]:  # run first 2 to keep test fast
            traj = run_eval_task(task)
            assert traj.task_id == task.id
            assert traj.error is None
