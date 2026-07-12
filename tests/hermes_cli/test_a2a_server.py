"""Tests for the A2A JSON-RPC server (issue #881, Slice 3 of #748).

Three layers:

* pure unit tests over ``hermes_cli.a2a_server`` helpers (envelope parsing,
  tool-call extraction, authorization, serialization) -- no web server, no
  registry;
* execution tests that register a STUB tool in the real
  ``tools.registry.registry`` and drive ``handle_rpc`` / ``sse_stream``
  directly (submit -> execute -> state), asserting no new *core* tool is
  needed -- the A2A layer only dispatches through the existing registry seam;
* an end-to-end test through the FastAPI route via ``TestClient`` covering
  submit -> execute -> state-over-SSE and the auth posture (a request without
  the session token is rejected 401 before any tool runs).

Two live Hermes instances are impractical in unit time, so the e2e maps to a
stub tool over the real route + real registry rather than a second process.
"""

from __future__ import annotations

import json

import pytest

from hermes_cli import a2a, a2a_server
from tools.registry import registry

STUB_TOOL = "a2a_stub_echo"


def _stub_handler(args, **kwargs):
    """Echo args back as a JSON string (a tool handler returns JSON text)."""
    return json.dumps({"echo": args})


def _stub_error_handler(args, **kwargs):
    return json.dumps({"error": "boom"})


@pytest.fixture(autouse=True)
def _register_stub_tool():
    """Register the stub in the real registry and advertise it on the card."""
    registry.register(
        name=STUB_TOOL,
        toolset="a2a-test",
        schema={"name": STUB_TOOL, "description": "echo stub"},
        handler=_stub_handler,
        override=True,
    )
    a2a.reset_discovery_cache()  # so _advertised_tools() sees the stub
    yield
    a2a.reset_discovery_cache()


def _send_payload(tool=STUB_TOOL, arguments=None, method="message/send", rid=1):
    return {
        "jsonrpc": "2.0",
        "id": rid,
        "method": method,
        "params": {
            "message": {
                "role": "user",
                "messageId": "m1",
                "parts": [
                    {
                        "kind": "data",
                        "data": {"tool": tool, "arguments": arguments or {}},
                    }
                ],
            }
        },
    }


# --------------------------------------------------------------------------
# Envelope / params parsing
# --------------------------------------------------------------------------


def test_parse_envelope_valid():
    rid, method, params, err = a2a_server._parse_envelope({
        "jsonrpc": "2.0",
        "id": 7,
        "method": "tasks/get",
        "params": {"id": "x"},
    })
    assert err is None
    assert (rid, method, params) == (7, "tasks/get", {"id": "x"})


def test_parse_envelope_rejects_non_object():
    _, _, _, err = a2a_server._parse_envelope([1, 2, 3])
    assert err.code == a2a_server.INVALID_REQUEST


def test_parse_envelope_rejects_bad_version():
    _, _, _, err = a2a_server._parse_envelope({"jsonrpc": "1.0", "method": "m"})
    assert err.code == a2a_server.INVALID_REQUEST


def test_parse_envelope_rejects_missing_method():
    _, _, _, err = a2a_server._parse_envelope({"jsonrpc": "2.0", "id": 1})
    assert err.code == a2a_server.INVALID_REQUEST


def test_parse_envelope_rejects_non_object_params():
    _, _, _, err = a2a_server._parse_envelope({
        "jsonrpc": "2.0",
        "method": "m",
        "params": [],
    })
    assert err.code == a2a_server.INVALID_PARAMS


# --------------------------------------------------------------------------
# Tool-call extraction
# --------------------------------------------------------------------------


def test_extract_tool_call_from_data_part():
    msg = {"parts": [{"kind": "data", "data": {"tool": "t", "arguments": {"a": 1}}}]}
    assert a2a_server._extract_tool_call(msg) == ("t", {"a": 1})


def test_extract_tool_call_from_metadata():
    msg = {"parts": [{"kind": "text", "text": "hi"}], "metadata": {"tool": "t"}}
    assert a2a_server._extract_tool_call(msg) == ("t", {})


def test_extract_tool_call_missing_mapping_raises():
    with pytest.raises(a2a_server.A2AError) as exc:
        a2a_server._extract_tool_call({"parts": [{"kind": "text", "text": "hi"}]})
    assert exc.value.code == a2a_server.INVALID_PARAMS


def test_extract_tool_call_rejects_bad_tool_type():
    with pytest.raises(a2a_server.A2AError):
        a2a_server._extract_tool_call({"metadata": {"tool": 123}})


def test_extract_tool_call_rejects_non_object_arguments():
    with pytest.raises(a2a_server.A2AError):
        a2a_server._extract_tool_call({"metadata": {"tool": "t", "arguments": [1]}})


# --------------------------------------------------------------------------
# Task model + store
# --------------------------------------------------------------------------


def test_task_to_dict_shape():
    task = a2a_server.A2ATask(id="i", context_id="c", tool="t", arguments={})
    task.result = json.dumps({"ok": True})
    a2a_server._set_state(task, a2a_server.COMPLETED)
    data = task.to_dict()
    assert data["id"] == "i"
    assert data["contextId"] == "c"
    assert data["kind"] == "task"
    assert data["status"]["state"] == "completed"
    assert data["artifacts"][0]["parts"][0]["text"] == json.dumps({"ok": True})
    # history captured the pre-completion (submitted) state.
    assert data["history"][0]["state"] == "submitted"


def test_taskstore_evicts_oldest():
    store = a2a_server.TaskStore(max_tasks=2)
    ids = []
    for _ in range(3):
        task = a2a_server.A2ATask(
            id=a2a_server._new_id(), context_id="c", tool="t", arguments={}
        )
        ids.append(task.id)
        store.put(task)
    assert store.get(ids[0]) is None  # first evicted
    assert store.get(ids[2]) is not None


# --------------------------------------------------------------------------
# Execution via handle_rpc / sse_stream (stub tool)
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_message_send_executes_tool_to_completion():
    store = a2a_server.TaskStore()
    resp = await a2a_server.handle_rpc(_send_payload(arguments={"a": 1}), store)
    task = resp["result"]
    assert task["status"]["state"] == "completed"
    echoed = json.loads(task["artifacts"][0]["parts"][0]["text"])
    assert echoed == {"echo": {"a": 1}}
    # Retrievable afterwards via tasks/get.
    got = await a2a_server.handle_rpc(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tasks/get",
            "params": {"id": task["id"]},
        },
        store,
    )
    assert got["result"]["id"] == task["id"]


@pytest.mark.asyncio
async def test_unadvertised_tool_is_rejected():
    resp = await a2a_server.handle_rpc(
        _send_payload(tool="definitely_not_registered_xyz"), a2a_server.TaskStore()
    )
    assert resp["error"]["code"] == a2a_server.METHOD_NOT_FOUND


@pytest.mark.asyncio
async def test_dispatch_error_envelope_marks_task_failed(monkeypatch):
    registry.register(
        name=STUB_TOOL,
        toolset="a2a-test",
        schema={"name": STUB_TOOL, "description": "err stub"},
        handler=_stub_error_handler,
        override=True,
    )
    resp = await a2a_server.handle_rpc(_send_payload(), a2a_server.TaskStore())
    assert resp["result"]["status"]["state"] == "failed"
    assert "boom" in resp["result"]["status"]["message"]["parts"][0]["text"]


@pytest.mark.asyncio
async def test_tasks_cancel_transitions_to_canceled():
    store = a2a_server.TaskStore()
    task = a2a_server.A2ATask(id="fixed", context_id="c", tool=STUB_TOOL, arguments={})
    store.put(task)
    resp = await a2a_server.handle_rpc(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tasks/cancel",
            "params": {"id": "fixed"},
        },
        store,
    )
    assert resp["result"]["status"]["state"] == "canceled"


@pytest.mark.asyncio
async def test_tasks_get_unknown_id_returns_task_not_found():
    resp = await a2a_server.handle_rpc(
        {"jsonrpc": "2.0", "id": 1, "method": "tasks/get", "params": {"id": "nope"}},
        a2a_server.TaskStore(),
    )
    assert resp["error"]["code"] == a2a_server.TASK_NOT_FOUND


@pytest.mark.asyncio
async def test_unknown_method_returns_method_not_found():
    resp = await a2a_server.handle_rpc(
        {"jsonrpc": "2.0", "id": 1, "method": "tasks/frobnicate", "params": {}},
        a2a_server.TaskStore(),
    )
    assert resp["error"]["code"] == a2a_server.METHOD_NOT_FOUND


@pytest.mark.asyncio
async def test_run_timeout_marks_failed(monkeypatch):
    import time as _time

    def _slow(args, **kwargs):
        _time.sleep(0.3)
        return json.dumps({"late": True})

    registry.register(
        name=STUB_TOOL,
        toolset="a2a-test",
        schema={"name": STUB_TOOL, "description": "slow stub"},
        handler=_slow,
        override=True,
    )
    monkeypatch.setattr(a2a_server, "TOOL_TIMEOUT_S", 0.01)
    task = a2a_server.A2ATask(id="t", context_id="c", tool=STUB_TOOL, arguments={})
    await a2a_server._run(task)
    assert task.state == "failed"
    assert "timed out" in (task.error or "")


@pytest.mark.asyncio
async def test_run_does_not_overwrite_a_canceled_task():
    """A cancel that landed while the tool was still running stays authoritative:
    the late tool result must NOT flip the task back to completed."""
    task = a2a_server.A2ATask(id="t", context_id="c", tool=STUB_TOOL, arguments={})
    a2a_server._set_state(task, a2a_server.CANCELED)  # cancel arrived first
    await a2a_server._run(task)  # tool finishes afterwards
    assert task.state == "canceled"
    assert task.result is None


@pytest.mark.asyncio
async def test_sse_stream_emits_submitted_working_completed():
    frames = []
    async for frame in a2a_server.sse_stream(
        _send_payload(method="message/stream", arguments={"k": "v"}),
        a2a_server.TaskStore(),
    ):
        assert frame.startswith("data: ") and frame.endswith("\n\n")
        frames.append(json.loads(frame[len("data: ") :]))
    states = [f["result"]["status"]["state"] for f in frames]
    assert states == ["submitted", "working", "completed"]
    assert frames[-1]["result"]["final"] is True
    echoed = json.loads(frames[-1]["result"]["artifacts"][0]["parts"][0]["text"])
    assert echoed == {"echo": {"k": "v"}}


# --------------------------------------------------------------------------
# End-to-end through the FastAPI route (TestClient)
# --------------------------------------------------------------------------

pytest.importorskip("starlette.testclient")
from starlette.testclient import TestClient  # noqa: E402

from hermes_cli import web_server  # noqa: E402


@pytest.fixture
def client():
    a2a.reset_discovery_cache()
    previous_auth_required = getattr(web_server.app.state, "auth_required", None)
    web_server.app.state.auth_required = False
    test_client = TestClient(web_server.app)
    test_client.headers[web_server._SESSION_HEADER_NAME] = web_server._SESSION_TOKEN
    try:
        yield test_client
    finally:
        a2a.reset_discovery_cache()
        if previous_auth_required is None:
            try:
                delattr(web_server.app.state, "auth_required")
            except AttributeError:
                pass
        else:
            web_server.app.state.auth_required = previous_auth_required


def test_e2e_message_send_over_route(client):
    resp = client.post("/a2a", json=_send_payload(arguments={"x": 9}))
    assert resp.status_code == 200
    task = resp.json()["result"]
    assert task["status"]["state"] == "completed"
    echoed = json.loads(task["artifacts"][0]["parts"][0]["text"])
    assert echoed == {"echo": {"x": 9}}


def test_e2e_message_stream_sse_over_route(client):
    resp = client.post("/a2a", json=_send_payload(method="message/stream"))
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    events = [
        json.loads(chunk[len("data: ") :])
        for chunk in resp.text.split("\n\n")
        if chunk.startswith("data: ")
    ]
    states = [e["result"]["status"]["state"] for e in events]
    assert states == ["submitted", "working", "completed"]


def test_e2e_requires_auth(client):
    """A request WITHOUT the session token is rejected before any tool runs."""
    unauth = TestClient(web_server.app)  # no session header
    resp = unauth.post("/a2a", json=_send_payload())
    assert resp.status_code == 401


def test_e2e_disabled_returns_404(client, monkeypatch):
    monkeypatch.setattr(a2a, "load_config", lambda *a, **k: {"enabled": False})
    a2a.reset_discovery_cache()
    resp = client.post("/a2a", json=_send_payload())
    assert resp.status_code == 404


def test_e2e_oversized_body_rejected(client):
    """A body over MAX_REQUEST_BYTES is rejected 413 before any parsing/dispatch."""
    big = b'{"jsonrpc":"2.0","id":1,"method":"message/send","params":{"x":"'
    big += b"A" * (a2a_server.MAX_REQUEST_BYTES + 16) + b'"}}'
    resp = client.post(
        "/a2a", content=big, headers={"content-type": "application/json"}
    )
    assert resp.status_code == 413
