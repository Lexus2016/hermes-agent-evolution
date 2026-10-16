"""Memory telemetry preserves dispatch outcomes, payloads and call-local isolation."""

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from hermes_cli.observability import shared_metrics_loop
from tools import memory_tool as memory


@pytest.fixture
def observations(monkeypatch):
    rows = []
    monkeypatch.setattr(shared_metrics_loop, "record_builtin_memory_call",
                        lambda action, operations, **fields: rows.append((action, fields)))
    monkeypatch.setattr(memory, "_apply_write_gate", lambda *args, **kwargs: None)
    return rows


def _store(add):
    return SimpleNamespace(target_enabled=lambda target: True, add=add)


@pytest.mark.parametrize("success", [False, True])
def test_store_result_is_unchanged_and_distinct_from_validation(observations, success):
    payload = {"success": success, "error": "private detail", "extra": ["preserved"]}

    def add(*args, **kwargs):
        memory.FAILURE_CLASS.set("invalid_args")
        return payload

    store = _store(add)
    result = memory.memory_tool("add", content="fact", store=store)
    assert result == json.dumps(payload, ensure_ascii=False)
    memory.memory_tool("explode", store=store)
    assert observations == [
        ("add", {"outcome": "success" if success else "failed",
                 "failure_class": "none" if success else "invalid_args"}),
        ("explode", {"outcome": "rejected", "failure_class": "invalid_args"}),
    ]


def test_staged_payload_stays_rejected_without_invoking_store(observations, monkeypatch):
    staged = '{"success": true, "staged": true, "pending_id": "private-id"}'

    def gate(*args, **kwargs):
        memory.FAILURE_CLASS.set("staged")
        return staged

    def add(*args, **kwargs):
        pytest.fail("A staged operation must not reach the store")

    monkeypatch.setattr(memory, "_apply_write_gate", gate)
    assert memory.memory_tool("add", content="fact", store=_store(add)) == staged
    assert observations == [("add", {"outcome": "rejected", "failure_class": "staged"})]


def test_nested_rejection_preserves_outer_store_failure(observations):
    def add(*args, **kwargs):
        memory.FAILURE_CLASS.set("no_match")
        memory.memory_tool("explode", store=store)
        return {"success": False, "error": "outer failure"}

    store = _store(add)
    memory.memory_tool("add", content="fact", store=store)
    assert observations == [
        ("explode", {"outcome": "rejected", "failure_class": "invalid_args"}),
        ("add", {"outcome": "failed", "failure_class": "no_match"}),
    ]


def test_concurrent_rejection_does_not_change_store_failure(observations):
    entered, release = threading.Event(), threading.Event()

    def add(*args, **kwargs):
        memory.FAILURE_CLASS.set("no_match")
        entered.set()
        assert release.wait(5)
        return {"success": False, "error": "private failure"}

    store = _store(add)
    with ThreadPoolExecutor(max_workers=1) as executor:
        pending = executor.submit(memory.memory_tool, "add", content="fact", store=store)
        try:
            assert entered.wait(5)
            memory.memory_tool("explode", store=store)
        finally:
            release.set()
        assert not json.loads(pending.result(timeout=5))["success"]
    assert observations == [
        ("explode", {"outcome": "rejected", "failure_class": "invalid_args"}),
        ("add", {"outcome": "failed", "failure_class": "no_match"}),
    ]


@pytest.mark.parametrize("action", ["add", "search"])
def test_exception_payload_or_propagation_and_context_restoration(observations, action):
    error = ValueError("private failure")

    def raises(*args, **kwargs):
        raise error

    store = _store(raises)
    store.search = raises
    class_token = memory.FAILURE_CLASS.set("no_match")
    outcome_token = memory._memory_call_outcome.set("failed")
    try:
        if action == "search":
            with pytest.raises(ValueError) as caught:
                memory.memory_tool(action, store=store)
            assert caught.value is error
            assert observations == []
        else:
            result = memory.memory_tool(action, content="fact", store=store)
            assert memory.FAILURE_CLASS.get() == "no_match"
            assert result == memory._memory_enriched_error(error, action)
            assert observations == [("add", {"outcome": "failed", "failure_class": "exception"})]
        assert memory._memory_call_outcome.get() == "failed"
        if action == "search":
            assert memory.FAILURE_CLASS.get() == "no_match"
    finally:
        memory._memory_call_outcome.reset(outcome_token)
        memory.FAILURE_CLASS.reset(class_token)
