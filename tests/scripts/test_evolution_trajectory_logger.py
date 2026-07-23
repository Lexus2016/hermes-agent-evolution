"""Tests for the cron-cycle trajectory logger (#1215)."""

import json
import os
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from scripts.evolution_trajectory_logger import (
    TrajectoryLogger,
    _redact_value,
    _truncate,
)


@pytest.fixture
def tmp_trajectory_dir(tmp_path, monkeypatch):
    """Redirect HERMES_HOME so trajectory sidecars land in a temp dir."""
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    return hermes_home


class TestRedactValue:
    """Secret-like keys must be redacted; non-secret values preserved."""

    def test_redacts_api_key(self):
        result = _redact_value({"api_key": "sk-12345", "model": "gpt-4"})
        assert result["api_key"] == "***REDACTED***"
        assert result["model"] == "gpt-4"

    def test_redacts_nested_token(self):
        result = _redact_value({"config": {"token": "abc", "name": "test"}})
        assert result["config"]["token"] == "***REDACTED***"
        assert result["config"]["name"] == "test"

    def test_redacts_list_elements(self):
        result = _redact_value([{"password": "secret"}, {"port": 8080}])
        assert result[0]["password"] == "***REDACTED***"
        assert result[1]["port"] == 8080

    def test_preserves_non_secret_string(self):
        assert _redact_value("hello world") == "hello world"

    def test_redacts_inline_pattern(self):
        result = _redact_value("api_key=sk-abc123")
        assert "sk-abc123" not in result
        assert "REDACTED" in result


class TestTruncate:
    """Truncation preserves content within the limit and adds an indicator."""

    def test_short_string_unchanged(self):
        assert _truncate("hello", 100) == "hello"

    def test_long_string_truncated(self):
        long_s = "x" * 200
        result = _truncate(long_s, 50)
        assert len(result) < 200
        assert result.endswith("...[truncated]")
        assert result[:50] == "x" * 50


class TestTrajectoryLogger:
    """Integration: logger writes a valid JSON sidecar with tool-call records."""

    def test_basic_logging(self, tmp_trajectory_dir):
        with TrajectoryLogger(session_id="test-001", job_name="unit-test") as tlog:
            tlog.log_tool_call(
                "terminal", {"command": "ls -la"}, "file1.txt\nfile2.py", True
            )
            tlog.log_tool_call(
                "read_file", {"path": "/tmp/x.py"}, "file contents", True
            )

        sidecar = tmp_trajectory_dir / "evolution" / "trajectories"
        files = list(sidecar.glob("*.json"))
        assert len(files) == 1
        data = json.loads(files[0].read_text())
        assert isinstance(data, list)
        assert len(data) == 1
        session = data[0]
        assert session["session_id"] == "test-001"
        assert session["job_name"] == "unit-test"
        assert session["tool_call_count"] == 2
        assert len(session["tool_calls"]) == 2
        assert session["tool_calls"][0]["tool"] == "terminal"
        assert session["tool_calls"][0]["success"] is True
        assert session["tool_calls"][1]["tool"] == "read_file"

    def test_secret_redaction_in_logged_args(self, tmp_trajectory_dir):
        with TrajectoryLogger(session_id="test-002") as tlog:
            tlog.log_tool_call(
                "web_search",
                {"api_key": "sk-secret-123", "query": "test"},
                "results",
                True,
            )
        sidecar = list(
            (tmp_trajectory_dir / "evolution" / "trajectories").glob("*.json")
        )[0]
        data = json.loads(sidecar.read_text())
        call = data[0]["tool_calls"][0]
        assert call["args"]["api_key"] == "***REDACTED***"
        assert call["args"]["query"] == "test"

    def test_failed_tool_call_records_error(self, tmp_trajectory_dir):
        with TrajectoryLogger(session_id="test-003") as tlog:
            tlog.log_tool_call(
                "terminal",
                {"command": "rm /"},
                "permission denied",
                False,
                error="PermissionError: cannot remove root",
            )
        sidecar = list(
            (tmp_trajectory_dir / "evolution" / "trajectories").glob("*.json")
        )[0]
        data = json.loads(sidecar.read_text())
        call = data[0]["tool_calls"][0]
        assert call["success"] is False
        assert "PermissionError" in call["error"]

    def test_multiple_sessions_append_to_same_file(self, tmp_trajectory_dir):
        from datetime import datetime, timezone

        date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        with TrajectoryLogger(session_id="s1", date_str=date_str) as tlog:
            tlog.log_tool_call("terminal", {"command": "echo hi"}, "hi", True)

        with TrajectoryLogger(session_id="s2", date_str=date_str) as tlog:
            tlog.log_tool_call("read_file", {"path": "/tmp/a"}, "content", True)

        sidecar = tmp_trajectory_dir / "evolution" / "trajectories" / f"{date_str}.json"
        data = json.loads(sidecar.read_text())
        assert len(data) == 2
        assert data[0]["session_id"] == "s1"
        assert data[1]["session_id"] == "s2"

    def test_close_is_idempotent(self, tmp_trajectory_dir):
        tlog = TrajectoryLogger(session_id="test-004")
        tlog.log_tool_call("terminal", {"command": "ls"}, "ok", True)
        tlog.close()
        tlog.close()  # should not duplicate or crash

        sidecar = list(
            (tmp_trajectory_dir / "evolution" / "trajectories").glob("*.json")
        )[0]
        data = json.loads(sidecar.read_text())
        assert len(data) == 1  # only one session record

    def test_log_after_close_is_ignored(self, tmp_trajectory_dir):
        tlog = TrajectoryLogger(session_id="test-005")
        tlog.log_tool_call("terminal", {"command": "ls"}, "ok", True)
        tlog.close()
        tlog.log_tool_call("write_file", {"path": "/tmp/x"}, "ok", True)

        sidecar = list(
            (tmp_trajectory_dir / "evolution" / "trajectories").glob("*.json")
        )[0]
        data = json.loads(sidecar.read_text())
        assert data[0]["tool_call_count"] == 1  # second call was ignored
