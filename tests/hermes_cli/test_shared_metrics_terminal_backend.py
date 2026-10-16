"""Backend execution metrics use raw outcomes, not enriched command error text."""

import json

import pytest

from hermes_cli.observability import shared_metrics_loop as metrics
from tests.hermes_cli.test_shared_metrics_loop import _rows
from tools import terminal_tool as terminal

pytest_plugins = [
    "tests.hermes_cli.test_relay_shared_metrics_runtime",
    "tests.tools.test_terminal_foreground_execution",
]


@pytest.mark.parametrize("result,expected", [
    ({"returncode": 0}, "none"), ({"returncode": 3}, "none"),
    ({"returncode": 124}, "none"), ({"returncode": -9}, "none"),
    ({"returncode": True}, None), ({"returncode": "0"}, None), ({}, None), ([], None),
    ({"yielded_session_id": "job"}, "none"), ({"yielded_session_id": True}, None),
    ({"yielded_session_id": ""}, None),
    ({"returncode": 0, "hermes_timed_out": True, "hermes_interrupted": True}, "timeout"),
    ({"returncode": 0, "hermes_interrupted": True}, "interrupted"),
    ({"returncode": 0, "hermes_timed_out": "true"}, "none"),
])
def test_raw_backend_outcome_uses_strict_shapes(result, expected):
    assert metrics.terminal_backend_error_class(result) == expected


@pytest.mark.parametrize("explicit,error,outcome", [
    ("none", "none", "success"), (None, "tool_error", "failed"),
    ("unrecognized", "unknown", "failed"),
])
def test_explicit_completion_differs_from_missing_observation(explicit, error, outcome):
    fields = metrics.execution_backend_fields(
        kind="terminal", backend="local", result={"exit_code": 3, "error": "private detail"},
        error_class=explicit,
    )
    assert fields == {"backend": "local", "kind": "terminal", "error_class": error, "outcome": outcome}


def _recorded(tmp_path):
    return _rows(tmp_path / "hermes-home", "hermes.execution_backend.count")


def _expected(error):
    return [({"backend": "local", "kind": "terminal", "error_class": error,
              "outcome": "success" if error == "none" else "failed"}, 1)]


@pytest.mark.usefixtures("direct_runtime")
@pytest.mark.parametrize("raw,error", [
    ({"output": "", "returncode": 3}, "none"),
    ({"output": "", "returncode": 124}, "none"),
    ({"output": "", "returncode": 124, "hermes_timed_out": True}, "timeout"),
    ({"output": "", "returncode": 130, "hermes_interrupted": True}, "interrupted"),
    ({"output": "started", "yielded_session_id": "job-123", "pid": 123}, "none"),
])
def test_public_foreground_caller_records_one_raw_outcome(foreground, tmp_path, raw, error):
    foreground.execute = lambda *args, **kwargs: dict(raw)
    result = json.loads(terminal.terminal_tool("exit 3", task_id="metric-contract"))
    if "returncode" in raw:
        assert result["exit_code"] == raw["returncode"]
    else:
        assert result["session_id"] == "job-123"
    assert _recorded(tmp_path) == _expected(error)


@pytest.mark.usefixtures("direct_runtime")
@pytest.mark.parametrize("exception,error", [
    (RuntimeError("Connection timeout"), "exception"),
    (TimeoutError("deadline exceeded"), "timeout"),
    (RuntimeError("backend unavailable"), "exception"),
])
def test_backend_exception_is_not_inferred_from_public_error(foreground, tmp_path, monkeypatch, exception, error):
    def execute(*args, **kwargs):
        raise exception

    foreground.execute = execute
    monkeypatch.setattr(terminal.time, "sleep", lambda seconds: None)
    result = json.loads(terminal.terminal_tool("echo ready", task_id="metric-error"))
    assert result["error"]
    assert _recorded(tmp_path) == _expected(error)


@pytest.mark.usefixtures("direct_runtime")
def test_final_retry_success_replaces_failure_without_double_counting(foreground, tmp_path, monkeypatch):
    outcomes = iter([RuntimeError("connection refused"),
                     {"output": "connection refused", "returncode": 1},
                     {"output": "ready", "returncode": 0}])
    attempts = []

    def execute(*args, **kwargs):
        outcome = next(outcomes)
        attempts.append(outcome)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    foreground.execute = execute
    monkeypatch.setattr(terminal.time, "sleep", lambda seconds: None)
    result = json.loads(terminal.terminal_tool("echo ready", task_id="metric-retry"))
    assert result["exit_code"] == 0
    assert result["output"] == "ready"
    assert len(attempts) == 3
    assert _recorded(tmp_path) == _expected("none")


@pytest.mark.usefixtures("direct_runtime")
def test_control_plane_call_remains_unmetered(foreground, tmp_path):
    foreground.execute = lambda *args, **kwargs: {"output": "", "returncode": 3}
    result = json.loads(terminal.terminal_tool("exit 3", task_id="metric-control", _host_local=True))
    assert result["exit_code"] == 3
    assert _recorded(tmp_path) == []


@pytest.mark.usefixtures("direct_runtime", "foreground")
def test_missing_observation_preserves_legacy_classification(tmp_path, monkeypatch):
    payload = '{"exit_code": 3, "error": "existing diagnostic"}'
    monkeypatch.setattr(terminal, "_run_foreground", lambda *args, **kwargs: payload)
    assert terminal.terminal_tool("exit 3", task_id="metric-unobserved") == payload
    assert _recorded(tmp_path) == _expected("tool_error")


@pytest.mark.usefixtures("direct_runtime")
def test_unrecognized_final_result_clears_earlier_retry_observation(foreground, tmp_path, monkeypatch):
    outcomes = iter([RuntimeError("connection refused"), {}])

    def execute(*args, **kwargs):
        outcome = next(outcomes)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    foreground.execute = execute
    monkeypatch.setattr(terminal.time, "sleep", lambda seconds: None)
    result = json.loads(terminal.terminal_tool("echo ready", task_id="metric-unknown"))
    assert result["exit_code"] == 0  # Preserve the formatter's existing missing-code default.
    assert _recorded(tmp_path) == _expected("none")
