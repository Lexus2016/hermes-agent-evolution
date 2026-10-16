"""Tests for session-trace replay / resume / branch (issue #85).

Exercises the pure functions in agent/tool_call_capture.py and the CLI in
scripts/evolution_trace_replay.py over both trajectory storage formats
(single-object .json from save(), per-turn .jsonl from append())."""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
for p in (str(ROOT / "scripts"), str(ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)

from agent.tool_call_capture import (
    branch_trajectory,
    replay_trajectory,
    resume_index,
)
from evolution_trajectory_logger import TrajectoryLog, iter_trajectories


def _make_log(*statuses: str, session_id: str = "s1") -> TrajectoryLog:
    log = TrajectoryLog(session_id=session_id, completed=True)
    for i, status in enumerate(statuses):
        log.add_tool_call(
            f"tool_{i}",
            {"arg": i},
            result=f"result {i}",
            status=status,
            duration_ms=i * 10,
        )
    return log


def _write_json(path: Path, log: TrajectoryLog) -> Path:
    """Write a trajectory in the single-object .json (save) format."""
    path.write_text(log.to_json(), encoding="utf-8")
    return path


def _save_path(tmp_path: Path, log: TrajectoryLog) -> Path:
    """Save a log and return its exact path (save() takes a directory and
    derives the filename from date + session_id; tmp_path also receives
    unrelated conftest artifacts, so never use iterdir())."""
    log.save(tmp_path)
    return tmp_path / f"{log.date}_{log.session_id}.json"


class TestIterTrajectories:
    def test_single_json_format(self, tmp_path):
        p = _save_path(tmp_path, _make_log("success", "success"))
        logs = iter_trajectories(p)
        assert len(logs) == 1
        assert [e.tool for e in logs[0].entries] == ["tool_0", "tool_1"]

    def test_jsonl_append_format_multi_turn(self, tmp_path):
        # Derive the filename from the log's own date (append() names the file
        # "{date}_{session_id}.jsonl") — a hardcoded date here breaks every PR
        # CI run after that date passes (issue #104).
        log = _make_log("success", session_id="s1")
        log.append(tmp_path)
        _make_log("failure", session_id="s1").append(tmp_path)
        p = tmp_path / f"{log.date}_s1.jsonl"
        logs = iter_trajectories(p)
        assert len(logs) == 2
        assert logs[0].entries[0].result_status == "success"
        assert logs[1].entries[0].result_status == "failure"

    def test_malformed_lines_skipped(self, tmp_path):
        p = tmp_path / "t.jsonl"
        p.write_text('{"session_id": "a", "entries": []}\nnot json\n', encoding="utf-8")
        logs = iter_trajectories(p)
        assert len(logs) == 1

    def test_missing_file_empty(self, tmp_path):
        assert iter_trajectories(tmp_path / "nope.json") == []

    def test_roundtrip_preserves_fields(self, tmp_path):
        p = _save_path(tmp_path, _make_log("success", "pending", session_id="s9"))
        log = iter_trajectories(p)[0]
        assert log.session_id == "s9"
        assert log.entries[1].result_status == "pending"


class TestReplayTrajectory:
    def test_all_success(self):
        r = replay_trajectory(_make_log("success", "success"))
        assert r["outcome"] == "success"
        assert r["step_count"] == 2
        assert r["first_failure_index"] is None
        assert r["stopped_at"] == 2

    def test_failure_detected(self):
        r = replay_trajectory(_make_log("success", "failure", "success"))
        assert r["outcome"] == "failed"
        assert r["first_failure_index"] == 1
        assert r["tool_sequence"] == ["tool_0", "tool_1", "tool_2"]

    def test_pending_is_incomplete(self):
        r = replay_trajectory(_make_log("success", "pending"))
        assert r["outcome"] == "incomplete"

    def test_window_clamps(self):
        r = replay_trajectory(_make_log("success", "failure"), start_at=1, stop_at=1)
        assert r["outcome"] == "empty"
        r2 = replay_trajectory(_make_log("success", "failure"), start_at=0, stop_at=1)
        assert r2["outcome"] == "success"
        assert r2["stopped_at"] == 1

    def test_error_status_counts_as_failure(self):
        r = replay_trajectory(_make_log("error"))
        assert r["outcome"] == "failed"

    def test_unknown_is_incomplete(self):
        r = replay_trajectory(_make_log("success", "unknown"))
        assert r["outcome"] == "incomplete"

    def test_duration_recorded_per_step(self):
        r = replay_trajectory(_make_log("success"))
        assert r["steps"][0]["duration_ms"] == 0


class TestResumeIndex:
    def test_clean_run_returns_none(self):
        assert resume_index(_make_log("success", "success")) is None

    def test_failure_point(self):
        assert resume_index(_make_log("success", "failure")) == 1

    def test_pending_point(self):
        assert resume_index(_make_log("pending", "success")) == 0

    def test_empty_log_returns_none(self):
        assert resume_index(TrajectoryLog(session_id="s")) is None


class TestBranchTrajectory:
    def test_prefix_inclusive(self):
        src = _make_log("success", "failure", "success")
        br = branch_trajectory(src, 1)
        assert len(br.entries) == 2
        assert [e.tool for e in br.entries] == ["tool_0", "tool_1"]
        assert br.entries[1].result_status == "failure"

    def test_source_not_mutated(self):
        src = _make_log("success", "failure")
        branch_trajectory(src, 0)
        assert len(src.entries) == 2
        assert src.entries[1].result_status == "failure"

    def test_clamped_upper_bound(self):
        src = _make_log("success", "success")
        br = branch_trajectory(src, 99)
        assert len(br.entries) == 2

    def test_zero_index_branches_first_step_only(self):
        src = _make_log("success", "failure")
        br = branch_trajectory(src, 0)
        assert len(br.entries) == 1

    def test_metadata_carried(self):
        src = _make_log("success", session_id="sx")
        src.completed = False
        br = branch_trajectory(src, 0)
        assert br.session_id == "sx"
        assert br.completed is False

    def test_branch_roundtrips_through_loader(self, tmp_path):
        src = _make_log("success", "failure", session_id="sb")
        br = branch_trajectory(src, 1)
        p = _save_path(tmp_path, br)
        loaded = iter_trajectories(p)[0]
        assert loaded.entries[1].result_status == "failure"


class TestTraceReplayCli:
    def _run(self, argv, capsys):
        from evolution_trace_replay import main

        code = main(["evolution_trace_replay.py", *argv])
        return code, capsys.readouterr()

    def test_replay_cli_json(self, tmp_path, capsys):
        p = _save_path(tmp_path, _make_log("success", "failure"))
        code, cap = self._run(["replay", str(p), "--json"], capsys)
        assert code == 0
        out = json.loads(cap.out)
        assert out["outcome"] == "failed"
        assert out["first_failure_index"] == 1

    def test_resume_cli(self, tmp_path, capsys):
        p = _save_path(tmp_path, _make_log("success", "pending"))
        code, cap = self._run(["resume", str(p)], capsys)
        assert code == 0
        assert "resume at index 1" in cap.out

    def test_resume_clean_run_exit_one(self, tmp_path, capsys):
        p = _save_path(tmp_path, _make_log("success"))
        code, cap = self._run(["resume", str(p)], capsys)
        assert code == 1
        assert "clean run" in cap.out

    def test_branch_cli_writes_file(self, tmp_path, capsys):
        p = _save_path(tmp_path, _make_log("success", "failure", "success"))
        out_dir = tmp_path / "branches"
        code, cap = self._run(
            ["branch", str(p), "--to", "1", "--out", str(out_dir)], capsys
        )
        assert code == 0
        written = Path(cap.out.strip())
        assert written.exists()
        loaded = iter_trajectories(written)[0]
        assert len(loaded.entries) == 2

    def test_bad_input_exit_two(self, tmp_path, capsys):
        code, cap = self._run(["replay", str(tmp_path / "missing.json")], capsys)
        assert code == 2
        assert "could not load" in cap.err
