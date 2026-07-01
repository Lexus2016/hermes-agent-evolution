"""Regression test for #647: foreground terminal_tool() calls must only pass
kwargs that BaseEnvironment.execute() actually accepts.

Local/SSH environments inherit BaseEnvironment.execute() unmodified — its
signature has no ``session_id``/``pty`` parameters. A prior change started
forwarding both into ``execute_kwargs`` whenever ``env_type`` was "local" or
"ssh", so every foreground call made with a non-None ``session_id`` (the
common case — callers pass it for durable observability) raised:

    TypeError: BaseEnvironment.execute() got an unexpected keyword
    argument 'session_id'

before the command was executed at all.
"""
import json
from types import SimpleNamespace

import tools.terminal_tool as terminal_tool_module


def _strict_execute(command, cwd="", *, timeout=None, stdin_data=None,
                     rewrite_compound_background=True):
    """Mirrors BaseEnvironment.execute()'s real signature: no session_id/pty."""
    return {"output": "ok", "returncode": 0}


def _base_config(tmp_path):
    return {
        "env_type": "local",
        "docker_image": "",
        "singularity_image": "",
        "modal_image": "",
        "daytona_image": "",
        "cwd": str(tmp_path),
        "timeout": 30,
    }


def test_foreground_execute_with_session_id_does_not_raise_typeerror(monkeypatch, tmp_path):
    config = _base_config(tmp_path)
    dummy_env = SimpleNamespace(cwd=str(tmp_path), execute=_strict_execute)

    monkeypatch.setattr(terminal_tool_module, "_get_env_config", lambda: config)
    monkeypatch.setattr(terminal_tool_module, "_start_cleanup_thread", lambda: None)
    monkeypatch.setattr(terminal_tool_module, "_check_all_guards", lambda *_a, **_k: {"approved": True})
    monkeypatch.setitem(terminal_tool_module._active_environments, "default", dummy_env)
    monkeypatch.setitem(terminal_tool_module._last_activity, "default", 0.0)

    try:
        result = json.loads(
            terminal_tool_module.terminal_tool(
                command="echo hi",
                session_id="conversation-123",
            )
        )
    finally:
        terminal_tool_module._active_environments.pop("default", None)
        terminal_tool_module._last_activity.pop("default", None)

    assert "TypeError" not in (result.get("error") or "")
    assert result.get("output") == "ok"


def test_foreground_execute_with_pty_does_not_raise_typeerror(monkeypatch, tmp_path):
    config = _base_config(tmp_path)
    dummy_env = SimpleNamespace(cwd=str(tmp_path), execute=_strict_execute)

    monkeypatch.setattr(terminal_tool_module, "_get_env_config", lambda: config)
    monkeypatch.setattr(terminal_tool_module, "_start_cleanup_thread", lambda: None)
    monkeypatch.setattr(terminal_tool_module, "_check_all_guards", lambda *_a, **_k: {"approved": True})
    monkeypatch.setitem(terminal_tool_module._active_environments, "default", dummy_env)
    monkeypatch.setitem(terminal_tool_module._last_activity, "default", 0.0)

    try:
        result = json.loads(
            terminal_tool_module.terminal_tool(
                command="echo hi",
                pty=True,
            )
        )
    finally:
        terminal_tool_module._active_environments.pop("default", None)
        terminal_tool_module._last_activity.pop("default", None)

    assert "TypeError" not in (result.get("error") or "")
    assert result.get("output") == "ok"
