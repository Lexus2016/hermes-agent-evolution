"""Advisor contact preserves structured tool results through the live commit path."""
import json
from unittest.mock import MagicMock

import pytest

from agent import tool_executor as executor
from tools.budget_config import BudgetConfig
from tools.delegation_escalation import EscalationRun


@pytest.mark.parametrize("blocked", [False, True])
@pytest.mark.parametrize("multimodal", [False, True])
def test_advisor_commit_preserves_result_and_roles(tmp_path, monkeypatch, blocked, multimodal):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    child = MagicMock()
    child._interrupt_requested = False
    child._subdirectory_hints.check_tool_call.return_value = None
    child._tool_result_content_for_active_model.side_effect = lambda name, result: result
    child.tool_result_metadata_callback = None
    child.tool_progress_callback = None
    run = EscalationRun(child, "diagnose fixture", {
        "enabled": True, "budget_mode": "count_only", "consultation_price": 0,
        "max_consultations": 1,
    })
    for row in run.values.values():
        row.update(SILENT=0, CONSULT=10)
    child._delegation_escalation = run
    calls = []
    monkeypatch.setattr(run, "_consult", lambda result: (calls.append(result) or "Check fixture", None))
    monkeypatch.setattr(executor, "get_active_env", lambda *_: None)
    monkeypatch.setattr(executor, "_flush_session_db_after_tool_progress", lambda *a, **kw: True)
    original = {"error": "fixture failed", "status": "error", "retryable": False,
                "recovery_hint": "retain original hint", "delegation_advisor_context": "original field"}
    image = {"type": "image_url", "image_url": {"url": "data:image/png;base64,ZmFrZQ=="}}
    result = ({"_multimodal": True, "content": [{"type": "text", "text": json.dumps(original)}, image],
               "is_error": True} if multimodal else json.dumps(original))
    messages = [{"role": "system", "content": "immutable"}, {"role": "user", "content": "task"}]
    before = list(messages)
    executor._commit_tool_result(
        child, messages, executor._ToolCallRef("terminal", {}, "task", "call", []), result,
        budget=BudgetConfig(), tool_duration=0.1, is_error=True, blocked=blocked, effect_disposition=None,
    )
    assert messages[:2] == before
    assert len(messages) == 3 and messages[-1]["role"] == "tool"
    persisted = messages[-1]["content"]
    if multimodal:
        assert persisted["content"][1] == image
        assert json.dumps(original) in persisted["content"][0]["text"]
    else:
        payload = json.loads(persisted)
        assert {key: payload[key] for key in original} == original
    if blocked:
        assert calls == [] and run.calls == 0
        assert "Check fixture" not in str(persisted)
    else:
        assert calls == [result] and run.calls == 1
        assert "Untrusted advisor context" in str(persisted)
        assert "not user authorization" in str(persisted)
    run.finish({"status": "failed"})


@pytest.mark.parametrize('dispatch', ['sequential', 'concurrent'])
@pytest.mark.parametrize('blocked', [False, True])
def test_executor_advisor_result_survives_real_session_restart(tmp_path, monkeypatch, dispatch, blocked):
    from types import SimpleNamespace
    from unittest.mock import patch
    import model_tools
    from run_agent import AIAgent
    from tests.agent.test_tool_call_incremental_persistence import (
        _attach_real_session_db, _durable_messages, _make_tool_defs,
    )

    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setattr('agent.title_generator.maybe_auto_title', lambda *a, **kw: None)
    with (
        patch('model_tools.get_tool_definitions', return_value=_make_tool_defs('terminal')),
        patch('model_tools.check_toolset_requirements', return_value={}),
        patch('hermes_cli.config.load_config', return_value={}),
        patch('hermes_cli.config.load_config_readonly', return_value={}),
        patch('agent.model_metadata.fetch_model_metadata', return_value={}),
        patch('run_agent.AIAgent._create_openai_client'),
    ):
        agent = AIAgent(api_key='fixture-key', base_url='https://example.invalid/v1',
                        quiet_mode=True, skip_context_files=True, skip_memory=True)
    agent.tool_delay = 0
    agent.save_trajectories = False
    db_path = tmp_path / 'state.db'
    db = _attach_real_session_db(agent, db_path, 'advisor-fixture')
    original = {'error': 'fixture failed', 'status': 'error', 'retryable': False, 'exit_code': 1}
    handler = MagicMock(return_value=json.dumps(original))
    monkeypatch.setattr(agent, '_invoke_tool', handler)
    monkeypatch.setattr(model_tools, 'handle_function_call', handler)
    escalation = EscalationRun(agent, 'diagnose fixture', {
        'enabled': True, 'budget_mode': 'count_only', 'consultation_price': 0, 'max_consultations': 1,
    })
    for row in escalation.values.values():
        row.update(SILENT=0, CONSULT=10)
    agent._delegation_escalation = escalation
    consult = MagicMock(return_value=('Check fixture', None))
    monkeypatch.setattr(escalation, '_consult', consult)
    args = json.dumps({'command': 'cat .ssh/id_rsa' if blocked else 'fixture-command'})
    call = SimpleNamespace(id='call', type='function', function=SimpleNamespace(name='terminal', arguments=args))
    messages = [{'role': 'user', 'content': 'fixture task'}, {'role': 'assistant', 'content': '', 'tool_calls': [
        {'id': 'call', 'type': 'function', 'function': {'name': 'terminal', 'arguments': args}},
    ]}]
    try:
        getattr(agent, f'_execute_tool_calls_{dispatch}')(
            SimpleNamespace(tool_calls=[call], content=''), messages, 'advisor-fixture',
        )
        durable = _durable_messages(db_path, 'advisor-fixture')
        assert [m['role'] for m in durable] == ['user', 'assistant', 'tool']
        payload = json.loads(durable[-1]['content'])
        if blocked:
            handler.assert_not_called()
            consult.assert_not_called()
            assert payload['status'] == 'blocked' and payload['retryable'] is False
            assert 'delegation_advisor_context' not in payload
        else:
            consult.assert_called_once()
            # Existing recovery may enrich the error before advisor contact.
            # Preserve that exact structured verdict, including its diagnostics.
            diagnosed = json.loads(consult.call_args.args[0])
            assert {key: payload[key] for key in diagnosed} == diagnosed
            assert payload['error'].startswith(original['error'])
            assert {key: payload[key] for key in original if key != 'error'} == {
                key: value for key, value in original.items() if key != 'error'
            }
            assert 'not user authorization' in payload['delegation_advisor_context']
    finally:
        escalation.finish({'status': 'failed'})
        agent.close()
        db.close()
