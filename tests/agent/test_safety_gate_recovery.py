"""Safety refusals remain attributable and never invite bypass (#3475)."""

import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest


@pytest.mark.parametrize("dispatch", ["sequential", "concurrent"])
def test_safety_refusal_stops_dispatch_and_survives_recovery(tmp_path, monkeypatch, dispatch):
    from agent import tool_executor as te
    from tools.tool_failure_classifier import classify_tool_failure
    from scripts.introspection_extract import _classify_failure_reason
    import model_tools
    from run_agent import AIAgent
    from agent.tool_guardrails import ToolCallGuardrailController
    from tools.recovery_strategy_dispatcher import recover_from_failure, RecoveryStrategy

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    report = home / "report.md"
    handler = Mock(side_effect=AssertionError("blocked call reached registry"))
    monkeypatch.setattr(model_tools, "handle_function_call", handler)
    agent = SimpleNamespace(
        _context_engine_tool_names=set(), _memory_manager=None, quiet_mode=False,
        _should_emit_quiet_tool_messages=lambda: False, _invoke_tool=Mock(),
    )
    for name, args in (
        ("write_file", {"path": str(report), "content": "Audit: protect .ssh/id_rsa"}),
        ("terminal", {"command": "cat .ssh/id_rsa"}),
    ):
        ref = te._ToolCallRef(name, args, "task", "call", [])
        if dispatch == "sequential":
            result = te._resolve_sequential_dispatch(agent, ref, []).execute(args)
        else:
            result = te._harm_gated_invoke(agent, ref, args, [])
        payload = json.loads(result)
        assert payload["status"] == "blocked"
        assert payload["harm_gate"] == "agent_process_bench"
        # The tag names the arm (#3542); the coarse prefix is the stable contract.
        assert payload["reason"].startswith("safety-gate-")
        assert payload["harm_class"] == "credential-access"
        assert "credential-access" in payload["error"]
        assert _classify_failure_reason(result) == payload["reason"]
        legacy = {key: payload[key] for key in ("error", "status", "harm_gate")}
        assert _classify_failure_reason(legacy) == payload["reason"]
        assert payload["retryable"] is False
        assert "approval" in payload["recovery_hint"]
        assert "Do not" in payload["recovery_hint"]
        observer = SimpleNamespace(
            _tool_guardrails=ToolCallGuardrailController(),
            _stall_guards_enabled=lambda: False,
            _failure_recovery_enabled=True,
        )
        observed = AIAgent._append_guardrail_observation(
            observer, name, args, result, failed=True,
        )
        assert "Non-retryable: safety_gate_blocked" in observed
        assert "Recovery strategy: surface_blocker" in observed
        assert "persistent_error" not in observed
        assert _classify_failure_reason(observed) == payload["reason"]
        classification = classify_tool_failure(name, result)
        assert classification.category.value == "safety_gate_blocked"
        assert classification.should_retry is False
        for streak in (0, 3, 5):
            recovery = recover_from_failure(name, result, consecutive_count=streak)
            assert recovery.strategy == RecoveryStrategy.surface_blocker
            assert not recovery.should_retry
            assert "approval" in recovery.directive
        assert not report.exists()
    agent._invoke_tool.assert_not_called()
    handler.assert_not_called()
    assert te._harm_gate_block_reason("write_file", {
        "path": str(report), "content": "Audit completed without findings",
    }) is None
    assert classify_tool_failure("write_file", "connection timed out").should_retry


@pytest.mark.parametrize("dispatch", ["sequential", "concurrent"])
def test_executor_publishes_terminal_refusal_as_failure(tmp_path, monkeypatch, dispatch):
    from unittest.mock import patch
    from run_agent import AIAgent
    import model_tools
    from scripts.introspection_extract import _classify_failure_reason
    from agent.display import _detect_tool_failure

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    config = {"tool_failure_recovery": {"enabled": True}}
    definitions = [{"type": "function", "function": {
        "name": name, "description": name,
        "parameters": {"type": "object", "properties": {}},
    }} for name in ("terminal", "write_file")]
    with (
        patch("model_tools.get_tool_definitions", return_value=definitions),
        patch("model_tools.check_toolset_requirements", return_value={}),
        patch("hermes_cli.config.load_config", return_value=config),
        patch("hermes_cli.config.load_config_readonly", return_value=config),
        patch("agent.process_bootstrap.OpenAI"),
    ):
        agent = AIAgent(
            api_key="test-key", base_url="https://example.invalid/v1",
            quiet_mode=True, skip_context_files=True, skip_memory=True,
        )
    agent.tool_delay = 0
    agent.save_trajectories = False
    handler = Mock(side_effect=AssertionError("blocked call reached handler"))
    monkeypatch.setattr(model_tools, "handle_function_call", handler)
    monkeypatch.setattr(agent, "_invoke_tool", handler)
    report = home / "report.md"
    calls = [SimpleNamespace(
        id=name, type="function",
        function=SimpleNamespace(name=name, arguments=json.dumps(args)),
    ) for name, args in (
        ("terminal", {"command": "cat .ssh/id_rsa"}),
        ("write_file", {"path": str(report), "content": "Audit: protect .ssh/id_rsa"}),
    )]
    messages = []
    execute = getattr(agent, f"_execute_tool_calls_{dispatch}")
    execute(SimpleNamespace(tool_calls=calls, content=""), messages, f"safety-{dispatch}")
    assert len(messages) == len(calls)
    for message in messages:
        result = message["content"]
        assert "Non-retryable: safety_gate_blocked" in result
        assert "Recovery strategy: surface_blocker" in result
        assert _classify_failure_reason(result) == "safety-gate-credential-access"
        assert _detect_tool_failure(message["tool_call_id"], result)[0]
        assert "exit_code" not in json.loads(result)  # no process was started
    handler.assert_not_called()
    assert not report.exists()
    assert not _detect_tool_failure("terminal", {"exit_code": 0, "output": "ok"})[0]
