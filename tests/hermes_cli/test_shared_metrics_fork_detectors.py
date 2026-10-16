"""Fork retry caps retain the bounded shared-metrics same-tool-failure family."""

import pytest

from hermes_cli.observability import shared_metrics_contract as contract
from tests.hermes_cli.test_shared_metrics_harness import _agent, _rows

pytest_plugins = ["tests.hermes_cli.test_relay_shared_metrics_runtime"]


@pytest.mark.usefixtures("direct_runtime")
@pytest.mark.parametrize("tool_name", ["terminal", "browser_navigate"])
def test_fork_guardrail_halt_reaches_same_tool_failure_metric(tmp_path, tool_name):
    from agent.tool_guardrails import ToolCallGuardrailConfig, ToolCallGuardrailController
    from run_agent import AIAgent

    agent = _agent()
    agent._tool_guardrails = ToolCallGuardrailController(ToolCallGuardrailConfig(hard_stop_enabled=True))
    agent._tool_guardrail_halt_decision = None
    agent._stall_guards_enabled = lambda: True
    agent._set_tool_guardrail_halt = lambda decision: AIAgent._set_tool_guardrail_halt(agent, decision)
    for i in range(8):
        AIAgent._append_guardrail_observation(
            agent, tool_name, {"command": "false"}, '{"exit_code": 1, "error": "boom"}', failed=True,
            tool_call_id=f"c{i}",
        )
    assert agent._tool_guardrail_halt_decision is not None
    signals = {(signal, detector) for signal, detector, _value in
               _rows(tmp_path, contract.LOOP_GUARD_METRIC, "signal", "detector")}
    assert ("loop_detected", "same_tool_failure") in signals
    assert any(signal == "repeated_tool_call" for signal, _detector in signals)
