"""Foreground registry dispatch preserves backend compatibility and shared retry budgets."""

import json
from types import SimpleNamespace

import pytest

from tools import terminal_tool as terminal
from tools.registry import registry


@pytest.fixture
def foreground(monkeypatch, tmp_path):
    env = SimpleNamespace(cwd=str(tmp_path), env={})
    config = {
        "env_type": "local", "cwd": str(tmp_path), "timeout": 30,
        "docker_image": "", "singularity_image": "", "modal_image": "",
        "daytona_image": "",
    }
    monkeypatch.setattr(terminal, "_get_env_config", lambda: config)
    monkeypatch.setattr(terminal, "_acquire_env", lambda *_args: env)
    monkeypatch.setattr(terminal, "_pre_exec_block", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(terminal, "_check_all_guards", lambda *_args, **_kwargs: {"approved": True})
    monkeypatch.setattr(terminal, "_terminal_streak_counts", {})
    monkeypatch.setattr(terminal, "_terminal_failure_repeats", {})
    return env


def _dispatch():
    return json.loads(registry.dispatch(
        "terminal", {"command": "echo ready", "timeout": 17}, task_id="foreground-contract",
    ))


def test_backend_without_yield_parameter_receives_supported_kwargs(foreground, monkeypatch):
    calls = []

    def execute(command, *, timeout, cwd, bounded_capture):
        calls.append((command, timeout, cwd, bounded_capture))
        return {"output": "ready", "returncode": 0}

    foreground.execute = execute
    monkeypatch.setattr(terminal, "_yield_kwargs", lambda *_args, **_kwargs: {"yield_handler": object()})

    result = _dispatch()

    assert result["exit_code"] == 0
    assert result["output"] == "ready"
    assert calls == [("echo ready", 17, foreground.cwd, True)]


@pytest.mark.parametrize("signature_kind", ["explicit", "variadic", "opaque"])
def test_supported_yield_handler_returns_tracked_background_result(foreground, monkeypatch, signature_kind):
    handler = object()
    received = []

    def yielding_execute(command, *, timeout, cwd, bounded_capture, yield_handler):
        received.append(yield_handler)
        return {"output": "started", "yielded_session_id": "job-123", "pid": 123}

    class OpaqueExecute:
        @property
        def __signature__(self):
            raise ValueError("Backend does not expose its signature")

        def __call__(self, command, **kwargs):
            return yielding_execute(command, **kwargs)

    def variadic_execute(command, **kwargs):
        return yielding_execute(command, **kwargs)

    foreground.execute = {
        "explicit": yielding_execute, "variadic": variadic_execute, "opaque": OpaqueExecute(),
    }[signature_kind]
    monkeypatch.setattr(terminal, "_yield_kwargs", lambda *_args, **_kwargs: {"yield_handler": handler})

    result = _dispatch()

    assert received == [handler]
    assert result["status"] == "yielded_to_background"
    assert result["session_id"] == "job-123"
    assert result["notify_on_complete"] is True
    assert result["output"] == "started"
    assert result["exit_code"] is None
    assert result["error"] is None
    assert result["pid"] == 123


def test_exceptions_and_nonzero_results_share_retry_budget(foreground, monkeypatch):
    attempts = []
    waits = []
    outcomes = iter([
        RuntimeError("connection refused"),
        {"output": "connection refused", "returncode": 1},
        RuntimeError("connection refused"),
        {"output": "ready", "returncode": 0},
    ])

    def execute(command, **kwargs):
        attempts.append((command, kwargs))
        outcome = next(outcomes)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    foreground.execute = execute
    monkeypatch.setattr(terminal.time, "sleep", waits.append)
    monkeypatch.setattr(terminal, "_yield_kwargs", lambda *_args, **_kwargs: {})

    result = _dispatch()

    assert result["output"] == "ready"
    assert result["exit_code"] == 0
    assert len(attempts) == 4
    assert waits == [2, 4, 8]
    assert terminal.get_terminal_streak("foreground-contract") == 0
    assert terminal.get_terminal_failure_repeat("foreground-contract") == 0
