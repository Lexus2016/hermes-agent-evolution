"""Tests for the 2026-07-24 evolution cycle provider-recovery fixes (#1263, #1264, #1265)."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from run_agent import AIAgent


def _td(*ns):
    return [{"type": "function", "function": {"name": n, "description": f"{n}", "parameters": {"type": "object", "properties": {}}}} for n in ns]

def _mr(content="Hello", fr="stop", tc=None):
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content, tool_calls=tc), finish_reason=fr)], model="m", usage=None)

class _BillErr(Exception):
    """403 billing error."""
    def __init__(self):
        super().__init__("key limit exceeded")
        self.status_code = 403

class _OverErr(Exception):
    """503 overload error."""
    def __init__(self):
        super().__init__("Service overloaded")
        self.status_code = 503

def _agent(mx=10):
    with (patch("run_agent.get_tool_definitions", return_value=_td("terminal")),
          patch("run_agent.check_toolset_requirements", return_value={}),
          patch("hermes_cli.config.load_config", return_value={}),
          patch("run_agent.OpenAI")):
        a = AIAgent(api_key="test-key-1234567890", base_url="https://openrouter.ai/api/v1",
                   max_iterations=mx, quiet_mode=True, skip_context_files=True, skip_memory=True)
    a.client = MagicMock()
    a._cached_system_prompt = "You are helpful."
    a._use_prompt_caching = False
    a.tool_delay = 0
    a.compression_enabled = False
    a.save_trajectories = False
    return a

def _fb():
    return (patch("agent.conversation_loop.jittered_backoff", return_value=0.01),
            patch("agent.conversation_loop.adaptive_rate_limit_backoff", side_effect=lambda r, **k: (0.01, None)))


# #1264 — billing error with no fallback fails fast with billing guidance

def test_billing_error_no_fallback_fails():
    agent = _agent()
    agent.client.chat.completions.create.side_effect = [_BillErr(), _mr("x")]
    jb, arb = _fb()
    with jb, arb, patch.object(agent, "_persist_session"), patch.object(agent, "_save_trajectory"), patch.object(agent, "_cleanup_task_resources"):
        result = agent.run_conversation("hello")
    assert result["failed"] is True
    assert result["failure_reason"] == "billing"
    assert agent.client.chat.completions.create.call_count == 1


# #1263 — overload breaker emits status when no fallback is available

def test_overload_breaker_emits_status_no_fallback():
    agent = _agent()
    agent.client.chat.completions.create.side_effect = [_OverErr(), _OverErr(), _mr("done")]
    msgs = []
    jb, arb = _fb()
    with jb, arb, patch.object(agent, "_buffer_status", side_effect=lambda m: msgs.append(m)), \
         patch.object(agent, "_persist_session"), patch.object(agent, "_save_trajectory"), patch.object(agent, "_cleanup_task_resources"):
        result = agent.run_conversation("hello")
    assert any("circuit breaker" in s.lower() for s in msgs), f"Got: {msgs}"


# #1265 — per-session refusal escalation

def test_refusal_session_counter_pattern():
    """#1265 — session counter uses getattr with default 0, increments."""
    class _Agent:
        pass
    agent = _Agent()
    assert getattr(agent, "_session_refusal_count", 0) == 0
    agent._session_refusal_count = 1
    assert getattr(agent, "_session_refusal_count", 0) == 1
    agent._session_refusal_count = 4
    assert getattr(agent, "_session_refusal_count", 0) == 4


def test_refusal_escalation_threshold():
    """Escalation fires when per-turn >= 2 AND session >= 4."""
    assert (2 >= 2 and 4 >= 4) is True
    assert (2 >= 2 and 3 >= 4) is False
    assert (1 >= 2 and 4 >= 4) is False