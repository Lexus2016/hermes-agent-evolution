"""Tests for the structured audit event log (#1718)."""

import json
from pathlib import Path

import pytest

from agent.audit_log import (
    EVENT_TOOL_CALL_BLOCKED,
    EVENT_TOOL_CALL_COMPLETE,
    _log_path,
    export_audit_jsonl,
    log_audit_event,
)


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    return tmp_path


def _enable(home: Path) -> None:
    (home / "config.yaml").write_text(
        "security:\n  audit_log: true\n", encoding="utf-8"
    )


def test_log_path_uses_hermes_home(home):
    assert _log_path() == home / "logs" / "audit-events.jsonl"


def test_disabled_by_default_writes_nothing(home):
    log_audit_event("tool_call_start", tool_name="x")
    assert not _log_path().exists()


def test_writes_jsonl_and_redacts(home):
    _enable(home)
    log_audit_event(
        EVENT_TOOL_CALL_COMPLETE,
        session_id="s1",
        turn_id="t1",
        tool_call_id="c1",
        tool_name="read_file",
        task_id="task-x",
        args={"path": "/etc/hosts"},
        result="ok",
        duration_ms=3,
        status="success",
        api_key="super-secret",
    )
    entry = json.loads(_log_path().read_text(encoding="utf-8"))
    assert entry["event"] == "tool_call_complete"
    assert entry["session_id"] == "s1" and entry["tool_name"] == "read_file"
    assert entry["status"] == "success" and "ts" in entry
    assert "api_key" not in entry


def test_export(home):
    _enable(home)
    log_audit_event(EVENT_TOOL_CALL_BLOCKED, tool_name="exec")
    out = home / "export.jsonl"
    assert export_audit_jsonl(str(out)) == str(out) and out.exists()
    text = export_audit_jsonl()
    assert "tool_call_blocked" in text and text.strip().endswith("}")


def test_write_failure_is_silent(home):
    _enable(home)
    (home / "logs").write_text("occupied", encoding="utf-8")
    log_audit_event(EVENT_TOOL_CALL_COMPLETE, tool_name="x")
