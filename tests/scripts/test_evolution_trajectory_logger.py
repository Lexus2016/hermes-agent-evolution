"""Tests for the trajectory logger module (issue #1215)."""

import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))


@pytest.fixture
def traj_env(tmp_path, monkeypatch):
    """Isolated trajectory environment with temp HERMES_HOME."""
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    return hermes_home


class TestTrajectoryLogger:
    """Test TrajectoryLogger library API."""

    def test_record_and_flush(self, traj_env):
        from scripts.evolution_trajectory_logger import TrajectoryLogger

        logger = TrajectoryLogger(session_id="test-session-1")
        logger.record("terminal", {"command": "ls -la"}, "file1.txt\nfile2.txt", "pass")
        logger.record("read_file", {"path": "/tmp/test.py"}, "contents here", "pass")
        assert logger.entry_count == 2

        path = logger.flush()
        assert path.exists()
        data = json.loads(path.read_text(encoding="utf-8"))
        assert len(data) == 1
        assert data[0]["session_id"] == "test-session-1"
        assert len(data[0]["entries"]) == 2
        assert data[0]["entries"][0]["tool"] == "terminal"
        assert data[0]["entries"][1]["tool"] == "read_file"

    def test_redact_secrets(self, traj_env):
        from scripts.evolution_trajectory_logger import TrajectoryLogger

        logger = TrajectoryLogger(session_id="test-secret")
        logger.record(
            "web_search",
            {"query": "test", "api_key": "sk-secret-123", "token": "abc"},
            "results",
            "pass",
        )
        path = logger.flush()
        data = json.loads(path.read_text(encoding="utf-8"))
        entry = data[0]["entries"][0]
        assert entry["args"]["api_key"] == "[REDACTED]"
        assert entry["args"]["token"] == "[REDACTED]"
        assert entry["args"]["query"] == "test"

    def test_truncate_long_result(self, traj_env):
        from scripts.evolution_trajectory_logger import TrajectoryLogger

        logger = TrajectoryLogger(session_id="test-trunc")
        long_result = "x" * 1000
        logger.record("terminal", {"command": "cat big.txt"}, long_result, "pass")
        path = logger.flush()
        data = json.loads(path.read_text(encoding="utf-8"))
        entry = data[0]["entries"][0]
        assert len(entry["result_summary"]) <= 500

    def test_flush_clears_entries(self, traj_env):
        from scripts.evolution_trajectory_logger import TrajectoryLogger

        logger = TrajectoryLogger(session_id="test-clear")
        logger.record("terminal", {"command": "ls"}, "ok", "pass")
        assert logger.entry_count == 1
        logger.flush()
        assert logger.entry_count == 0

    def test_multiple_sessions_same_date(self, traj_env):
        from scripts.evolution_trajectory_logger import TrajectoryLogger

        logger1 = TrajectoryLogger(session_id="session-a")
        logger1.record("terminal", {"command": "ls"}, "ok", "pass")
        logger1.flush()

        logger2 = TrajectoryLogger(session_id="session-b")
        logger2.record("read_file", {"path": "/x"}, "data", "pass")
        logger2.flush()

        # Both should be in the same dated file
        from scripts.evolution_trajectory_logger import _get_trajectories_dir

        files = list(_get_trajectories_dir().glob("*.json"))
        assert len(files) == 1
        data = json.loads(files[0].read_text(encoding="utf-8"))
        assert len(data) == 2
        assert data[0]["session_id"] == "session-a"
        assert data[1]["session_id"] == "session-b"


class TestTrajectoryLoggerCLI:
    """Test the CLI entry point (main())."""

    def test_cli_record_and_flush(self, traj_env):
        from scripts.evolution_trajectory_logger import main

        # Record a tool call
        rc = main([
            "--session",
            "cli-test",
            "--tool-call",
            "--tool-name",
            "terminal",
            "--args",
            '{"command": "echo hello"}',
            "--result",
            "hello",
            "--status",
            "pass",
            "--flush",
        ])
        assert rc == 0

        # Verify the file was written
        from scripts.evolution_trajectory_logger import _get_trajectories_dir

        files = list(_get_trajectories_dir().glob("*.json"))
        assert len(files) == 1
        data = json.loads(files[0].read_text(encoding="utf-8"))
        assert data[0]["session_id"] == "cli-test"
        assert data[0]["entries"][0]["tool"] == "terminal"
        assert data[0]["entries"][0]["args"]["command"] == "echo hello"

    def test_cli_invalid_args_json_handled(self, traj_env):
        from scripts.evolution_trajectory_logger import main

        rc = main([
            "--session",
            "cli-bad-json",
            "--tool-call",
            "--tool-name",
            "test",
            "--args",
            "not valid json",
            "--result",
            "ok",
            "--status",
            "pass",
            "--flush",
        ])
        assert rc == 0
        from scripts.evolution_trajectory_logger import _get_trajectories_dir

        files = list(_get_trajectories_dir().glob("*.json"))
        data = json.loads(files[0].read_text(encoding="utf-8"))
        assert data[0]["entries"][0]["args"]["raw"] == "not valid json"

    def test_cli_no_args_prints_help(self, traj_env, capsys):
        from scripts.evolution_trajectory_logger import main

        rc = main(["--session", "x"])
        assert rc == 1
