"""Runtime and offline contracts for #3461; fixtures are not quality benchmarks."""
import json
from types import SimpleNamespace
from unittest.mock import MagicMock

from tools.delegation_escalation import EscalationRun, run_with_escalation
from scripts.delegation_escalation_report import fit, runs, summarize


def test_real_result_commit_preserves_roles_and_denials(tmp_path, monkeypatch):
    from agent import tool_executor as executor
    from tools.budget_config import BudgetConfig

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    child = MagicMock()
    child._interrupt_requested = False
    child._subdirectory_hints.check_tool_call.return_value = None
    child._tool_result_content_for_active_model.side_effect = lambda name, result: result
    child.tool_result_metadata_callback = None
    child.tool_progress_callback = None
    child._flush_messages_to_session_db.return_value = True
    table = tmp_path / "evolution" / "delegation-action-values.json"
    table.parent.mkdir(parents=True)
    table.write_text(json.dumps({"schema": "delegation-action-values-v1",
        "values": {"start": {"SILENT": 0, "CONSULT": 10}}}))
    run = EscalationRun(child, "private task", {"enabled": True, "budget_mode": "count_only", "consultation_price": 0, "max_consultations": 1})
    assert run.contacts[0]["reason"] == "start_observation"
    assert run.calls == 0
    child._delegation_escalation = run
    calls = []
    monkeypatch.setattr(run, "_consult", lambda result: (calls.append(result) or "Check the fixture", None))
    monkeypatch.setattr(executor, "get_active_env", lambda *_: None)
    monkeypatch.setattr(executor, "_flush_session_db_after_tool_progress", lambda *a, **kw: True)
    messages = [{"role": "system", "content": "immutable"}, {"role": "assistant", "content": "", "tool_calls": []}]
    for index, blocked in enumerate([True, False, False]):
        ref = executor._ToolCallRef("terminal", {}, "task", str(index), [])
        executor._commit_tool_result(child, messages, ref, '{"error": "failed fixture"}',
            budget=BudgetConfig(), tool_duration=0.1, is_error=True, blocked=blocked,
            effect_disposition=None)
    assert len(calls) == 1
    assert run.contacts[1]["reason"] == "safety_denial"
    assert run.contacts[-1]["reason"] == "call_budget"
    assert messages[0]["content"] == "immutable"
    assert all(m["role"] == "tool" for m in messages[2:])
    assert "Untrusted advisor" in messages[3]["content"]
    entry = run.finish({"status": "completed"})
    assert entry["escalation"]["added_cost_usd"] is None
    assert entry["escalation"]["recovered_failures_vs_always_proceed"] is None
    trace, report = next(runs(tmp_path / "evolution" / "trajectories"))
    assert report["contacts"] == len(run.contacts)
    directory = tmp_path / "evolution" / "trajectories"
    assert summarize(directory)["recovered_failures_vs_always_proceed"] is None
    reviewed = {trace["session_id"]: {"verified": True, "reward": 1,
        "paired_baseline_verified": True, "baseline_reward": 0,
        "baseline_evidence": "controlled fixture pair"}}
    assert summarize(directory, verified_outcomes=reviewed)["recovered_failures_vs_always_proceed"] == 1
    assert "private task" not in json.dumps(trace)
    assert "failed fixture" not in json.dumps(trace)


def test_default_runs_profile_isolation_and_verified_learning(tmp_path, monkeypatch):
    from tools import delegate_tool
    monkeypatch.setattr(delegate_tool, "_load_config", dict)
    monkeypatch.setattr(EscalationRun, "_consult", lambda *_: (_ for _ in ()).throw(AssertionError("default must not spend")))
    paths = [tmp_path / "a", tmp_path / "b", tmp_path / "a"]
    for home, invalid in zip(paths, [[], {"schema": "delegation-action-values-v1", "values": []}, {"values": None}]):
        home.joinpath("evolution").mkdir(parents=True, exist_ok=True)
        home.joinpath("evolution/delegation-action-values.json").write_text(json.dumps(invalid))
        monkeypatch.setenv("HERMES_HOME", str(home))
        child = SimpleNamespace(_interrupt_requested=False)
        def execute(*args, **kwargs):
            child._delegation_escalation.contact("terminal", '{"error": "failed"}')
            return {"status": "failed"}
        result = run_with_escalation(execute, 0, "secret goal", child)
        assert result["escalation"]["consultations"] == 0
        assert child._delegation_escalation is None
    directory = paths[0] / "evolution" / "trajectories"
    assert summarize(directory)["runs"] == 2
    assert summarize(paths[1] / "evolution" / "trajectories")["runs"] == 1
    assert not fit(directory, {}, 0.5)["values"]
    data, _ = next(runs(directory))
    outcomes = {data["session_id"]: {"verified": True, "reward": 1}}
    learned = fit(directory, outcomes, 0.5)
    assert learned["values"]["failure"]["SILENT"] == 1
    assert fit(directory, outcomes, 0.5) == learned


def test_capacity_failure_never_uses_recovery_or_main_route(tmp_path, monkeypatch):
    from agent import auxiliary_client as aux
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    child = SimpleNamespace(_interrupt_requested=False)
    run = EscalationRun(child, "diagnose", {"enabled": True, "budget_mode": "count_only", "consultation_price": 0})
    route = {"provider": "openai", "model": "configured-advisor"}
    monkeypatch.setattr(aux, "_get_auxiliary_task_config", lambda _: route)
    client = MagicMock()
    client.with_options.return_value = client
    client.chat.completions.create.side_effect = RuntimeError("capacity exhausted")
    monkeypatch.setattr(aux, "get_text_auxiliary_client", lambda _: (client, route["model"]))
    monkeypatch.setattr(aux, "_try_main_fallback_chain", lambda *a, **kw: (_ for _ in ()).throw(AssertionError("main route forbidden")))
    monkeypatch.setattr(aux, "_try_configured_fallback_chain", lambda *a, **kw: (_ for _ in ()).throw(AssertionError("fallback forbidden")))
    assert run.contact("terminal", '{"error": "failed"}') == ""
    assert client.chat.completions.create.call_count == 1
    client.with_options.assert_called_once_with(max_retries=0, timeout=30)
    assert run.contacts[-1]["consultation_error"] is True
    assert run.contact("terminal", '{"error": "failed"}') == ""
    assert client.chat.completions.create.call_count == 1


def test_safety_gate_envelope_cannot_consume_advisor_budget(tmp_path, monkeypatch):
    from agent.tool_executor import _harm_blocked_tool_result

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    run = EscalationRun(SimpleNamespace(_interrupt_requested=False), "diagnose", {
        "enabled": True, "budget_mode": "count_only", "consultation_price": 0,
        "max_consultations": 10,
    })
    # Even a learned preference for advice cannot turn a refusal into a consult.
    for row in run.values.values():
        row.update(SILENT=0, CONSULT=10)
    requests = []
    monkeypatch.setattr(run, "_consult", lambda result: (requests.append(result) or "advice", None))
    for _ in range(3):
        assert run.contact("terminal", _harm_blocked_tool_result("risk flags"), blocked=False) == ""
    assert requests == []
    assert run.calls == 0
    assert all(c["reason"] == "safety_denial" for c in run.contacts[1:])
    assert all(not c["consultation_attempted"] for c in run.contacts)
