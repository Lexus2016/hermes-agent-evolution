"""Tests for the A2A task lifecycle client (``hermes_cli/a2a_client.py``).

Everything runs against an in-memory mock A2A server built on
:class:`httpx.MockTransport` -- no real network. The mock server implements
the A2A JSON-RPC surface the client speaks (``tasks/send``, ``tasks/get``,
``tasks/cancel`` and the SSE ``tasks/sendSubscribe``) plus the
``/.well-known/agent.json`` discovery document, records every request, and
lets each test script the exact state sequence it wants to observe.
"""

from __future__ import annotations

import json
from typing import Any, Callable, Dict, List, Optional

import httpx
import pytest

from hermes_cli.a2a import AgentSkill
from hermes_cli.a2a_client import (
    A2AClient,
    A2AClientError,
    A2AProtocolError,
    Task,
    TaskState,
    TaskStatusUpdate,
    _iter_sse,
    text_message,
)

BASE_URL = "http://peer.test"


# --------------------------------------------------------------------------
# In-memory mock A2A server
# --------------------------------------------------------------------------


def _sse(*frames: Dict[str, Any]) -> bytes:
    """Serialise JSON-RPC result frames as an SSE ``text/event-stream`` body."""
    chunks = []
    for frame in frames:
        chunks.append("data: " + json.dumps(frame) + "\n\n")
    return "".join(chunks).encode("utf-8")


def _status_frame(
    request_id: Any, task_id: str, state: str, *, final: bool = False
) -> Dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "result": {"id": task_id, "status": {"state": state}, "final": final},
    }


class MockA2AServer:
    """Scriptable A2A peer served through an ``httpx.MockTransport`` handler.

    ``send_result`` / ``get_result`` / ``cancel_result`` return the JSON-RPC
    ``result`` object for the matching method; ``stream_body`` returns the raw
    SSE bytes for ``tasks/sendSubscribe``. Each is overridable per test.
    """

    def __init__(self) -> None:
        self.requests: List[httpx.Request] = []
        self.bodies: List[Dict[str, Any]] = []
        self.card: Dict[str, Any] = {
            "name": "PeerBot",
            "url": "/a2a",  # relative -> resolved against BASE_URL
            "version": "1.0.0",
            "capabilities": {"streaming": True},
            "authentication": {"schemes": ["bearer"]},
            "skills": [
                {
                    "id": "echo",
                    "name": "echo",
                    "description": "Echo text",
                    "tags": ["x"],
                }
            ],
        }
        self.send_result: Callable[[str, Dict[str, Any]], Dict[str, Any]] = (
            lambda tid, params: {"id": tid, "status": {"state": "completed"}}
        )
        self.get_result: Callable[[str, Dict[str, Any]], Dict[str, Any]] = (
            lambda tid, params: {"id": tid, "status": {"state": "working"}}
        )
        self.cancel_result: Callable[[str, Dict[str, Any]], Dict[str, Any]] = (
            lambda tid, params: {"id": tid, "status": {"state": "canceled"}}
        )
        self.stream_body: Optional[Callable[[Any, str], bytes]] = None
        self.card_status: int = 200

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def client(self, **kwargs: Any) -> A2AClient:
        return A2AClient(BASE_URL, transport=self.transport, **kwargs)

    # -- request dispatch --------------------------------------------------

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path == "/.well-known/agent.json":
            if self.card_status != 200:
                return httpx.Response(self.card_status, text="nope")
            return httpx.Response(200, json=self.card)

        body = json.loads(request.content.decode("utf-8"))
        self.bodies.append(body)
        method = body.get("method")
        params = body.get("params") or {}
        request_id = body.get("id")
        task_id = str(params.get("id", ""))

        if method == "tasks/send":
            return self._result(request_id, self.send_result(task_id, params))
        if method == "tasks/get":
            return self._result(request_id, self.get_result(task_id, params))
        if method == "tasks/cancel":
            return self._result(request_id, self.cancel_result(task_id, params))
        if method == "tasks/sendSubscribe":
            builder = self.stream_body or self._default_stream
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=builder(request_id, task_id),
            )
        return self._error(request_id, -32601, f"method not found: {method}")

    def _default_stream(self, request_id: Any, task_id: str) -> bytes:
        return _sse(
            _status_frame(request_id, task_id, "submitted"),
            _status_frame(request_id, task_id, "working"),
            _status_frame(request_id, task_id, "completed", final=True),
        )

    @staticmethod
    def _result(request_id: Any, result: Dict[str, Any]) -> httpx.Response:
        return httpx.Response(
            200, json={"jsonrpc": "2.0", "id": request_id, "result": result}
        )

    @staticmethod
    def _error(request_id: Any, code: int, message: str) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": code, "message": message},
            },
        )


@pytest.fixture
def server() -> MockA2AServer:
    return MockA2AServer()


# --------------------------------------------------------------------------
# Pure helpers: state coercion + SSE parsing
# --------------------------------------------------------------------------


def test_task_state_from_wire_normalises_spellings():
    assert TaskState.from_wire("submitted") is TaskState.SUBMITTED
    assert TaskState.from_wire("input-required") is TaskState.INPUT_REQUIRED
    # Both the A2A ("canceled") and British ("cancelled") spellings map home.
    assert TaskState.from_wire("canceled") is TaskState.CANCELED
    assert TaskState.from_wire("cancelled") is TaskState.CANCELED
    assert TaskState.from_wire("  WORKING ") is TaskState.WORKING
    # Unknown / empty never raises -- it degrades to the sentinel.
    assert TaskState.from_wire("wat") is TaskState.UNKNOWN
    assert TaskState.from_wire(None) is TaskState.UNKNOWN


def test_task_state_terminal_flags():
    assert TaskState.COMPLETED.is_terminal
    assert TaskState.CANCELED.is_terminal
    assert TaskState.FAILED.is_terminal
    assert not TaskState.WORKING.is_terminal
    assert not TaskState.INPUT_REQUIRED.is_terminal


def test_iter_sse_multiline_comments_and_compact():
    lines = iter([
        ": keep-alive comment",
        "event: status",
        "data: line-one",
        "data: line-two",
        "",  # dispatch first event
        'data:{"compact":true}',  # no space after colon
        "",  # dispatch second event
    ])
    events = list(_iter_sse(lines, max_events=100))
    assert events[0] == ("status", "line-one\nline-two")
    assert events[1] == (None, '{"compact":true}')


def test_iter_sse_flushes_trailing_event_without_blank_line():
    events = list(_iter_sse(iter(["data: only\r"]), max_events=100))
    assert events == [(None, "only")]


def test_iter_sse_respects_max_events():
    lines = iter(["data: a", "", "data: b", "", "data: c", ""])
    assert list(_iter_sse(lines, max_events=2)) == [(None, "a"), (None, "b")]


def test_iter_sse_aborts_on_unterminated_event_buffer():
    from hermes_cli import a2a_client

    # A peer that streams endless ``data:`` lines with no blank line would grow
    # the buffer without bound; the per-event cap turns that into a clean abort.
    def endless():
        while True:
            yield "data: x"

    original = a2a_client._MAX_DATA_LINES_PER_EVENT
    a2a_client._MAX_DATA_LINES_PER_EVENT = 5
    try:
        with pytest.raises(A2AClientError):
            list(_iter_sse(endless(), max_events=100))
    finally:
        a2a_client._MAX_DATA_LINES_PER_EVENT = original


def test_text_message_shape():
    assert text_message("hi") == {
        "role": "user",
        "parts": [{"type": "text", "text": "hi"}],
    }


# --------------------------------------------------------------------------
# Discovery
# --------------------------------------------------------------------------


def test_discover_parses_card_and_resolves_relative_endpoint(server):
    agent = server.client().discover()
    assert agent.name == "PeerBot"
    assert agent.endpoint == "http://peer.test/a2a"  # relative -> absolute
    assert agent.supports_streaming is True
    assert agent.skills == [
        AgentSkill(id="echo", name="echo", description="Echo text", tags=["x"])
    ]


def test_discover_leaves_absolute_endpoint_untouched(server):
    server.card["url"] = "https://pinned.example/rpc"
    agent = server.client().discover()
    assert agent.endpoint == "https://pinned.example/rpc"


def test_discover_http_error_raises_client_error(server):
    server.card_status = 500
    with pytest.raises(A2AClientError):
        server.client().discover()


# --------------------------------------------------------------------------
# discover -> invoke
# --------------------------------------------------------------------------


def test_send_task_auto_discovers_then_invokes(server):
    """A bare send_task must first GET the card, then POST tasks/send."""
    task = server.client().send_task(text_message("ping"), task_id="t-1")

    assert isinstance(task, Task)
    assert task.state is TaskState.COMPLETED
    assert task.id == "t-1"

    paths = [r.url.path for r in server.requests]
    assert paths == ["/.well-known/agent.json", "/a2a"]  # discover then invoke

    sent = server.bodies[0]
    assert sent["method"] == "tasks/send"
    assert sent["jsonrpc"] == "2.0"
    assert sent["params"]["id"] == "t-1"
    assert sent["params"]["message"] == text_message("ping")


def test_send_task_attaches_bearer_token(server):
    server.client(auth_token="secret-abc").send_task(text_message("hi"), task_id="t")
    invoke = [r for r in server.requests if r.url.path == "/a2a"][0]
    assert invoke.headers["Authorization"] == "Bearer secret-abc"


def test_send_task_reuses_discovered_endpoint(server):
    client = server.client()
    client.discover()
    client.send_task(text_message("a"), task_id="t-a")
    client.send_task(text_message("b"), task_id="t-b")
    # One discovery, two invokes -- endpoint cached after first discover.
    paths = [r.url.path for r in server.requests]
    assert paths.count("/.well-known/agent.json") == 1
    assert paths.count("/a2a") == 2


def test_send_task_input_required_state(server):
    server.send_result = lambda tid, params: {
        "id": tid,
        "status": {"state": "input-required", "message": {"role": "agent"}},
    }
    task = server.client().send_task(text_message("need more"), task_id="t")
    assert task.state is TaskState.INPUT_REQUIRED
    assert task.status.message == {"role": "agent"}


# --------------------------------------------------------------------------
# SSE subscribe: observable state transitions
# --------------------------------------------------------------------------


def test_subscribe_observes_full_transition_sequence(server):
    updates = list(server.client().subscribe(text_message("go"), task_id="job-1"))
    assert [u.state for u in updates] == [
        TaskState.SUBMITTED,
        TaskState.WORKING,
        TaskState.COMPLETED,
    ]
    assert all(isinstance(u, TaskStatusUpdate) for u in updates)
    assert all(u.task_id == "job-1" for u in updates)
    assert updates[-1].final is True

    # The subscribe call went to the JSON-RPC endpoint with the SSE method.
    subscribe_body = server.bodies[-1]
    assert subscribe_body["method"] == "tasks/sendSubscribe"


def test_subscribe_stops_on_terminal_state_without_final_flag(server):
    # Server never sets ``final``; the client must still stop on ``canceled``.
    def stream(request_id, task_id):
        return _sse(
            _status_frame(request_id, task_id, "working"),
            _status_frame(request_id, task_id, "canceled"),
            _status_frame(request_id, task_id, "working"),  # must never surface
        )

    server.stream_body = stream
    updates = list(server.client().subscribe(text_message("go"), task_id="j"))
    assert [u.state for u in updates] == [TaskState.WORKING, TaskState.CANCELED]


def test_subscribe_skips_artifact_frames_without_status(server):
    def stream(request_id, task_id):
        artifact = {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {"id": task_id, "artifact": {"parts": []}},
        }
        return (
            _sse(_status_frame(request_id, task_id, "working"))
            + _sse(artifact)
            + _sse(_status_frame(request_id, task_id, "completed", final=True))
        )

    server.stream_body = stream
    updates = list(server.client().subscribe(text_message("go"), task_id="j"))
    assert [u.state for u in updates] == [TaskState.WORKING, TaskState.COMPLETED]


def test_subscribe_early_break_closes_stream_without_hang(server):
    def stream(request_id, task_id):
        return _sse(
            _status_frame(request_id, task_id, "submitted"),
            _status_frame(request_id, task_id, "working"),
            _status_frame(request_id, task_id, "completed", final=True),
        )

    server.stream_body = stream
    gen = server.client().subscribe(text_message("go"), task_id="j")
    first = next(gen)
    assert first.state is TaskState.SUBMITTED
    # Closing the generator early must tear down the stream cleanly (the
    # generator's ``with`` blocks run on GeneratorExit) -- no exception, no hang.
    gen.close()


def test_subscribe_max_events_cap_prevents_unbounded_stream(server):
    def stream(request_id, task_id):
        # 5 non-final ``working`` frames -- a server that never terminates.
        return _sse(*[_status_frame(request_id, task_id, "working") for _ in range(5)])

    server.stream_body = stream
    client = server.client(max_stream_events=3)
    updates = list(client.subscribe(text_message("go"), task_id="j"))
    assert len(updates) == 3  # hard cap, not all 5


def test_subscribe_protocol_error_frame_raises(server):
    def stream(request_id, task_id):
        return _sse({
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": -32000, "message": "boom"},
        })

    server.stream_body = stream
    with pytest.raises(A2AProtocolError) as exc:
        list(server.client().subscribe(text_message("go"), task_id="j"))
    assert exc.value.code == -32000


def test_subscribe_http_error_raises_client_error(server):
    def broken_transport(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/.well-known/agent.json":
            return httpx.Response(200, json=server.card)
        return httpx.Response(503, text="unavailable")

    client = A2AClient(BASE_URL, transport=httpx.MockTransport(broken_transport))
    with pytest.raises(A2AClientError):
        list(client.subscribe(text_message("go"), task_id="j"))


# --------------------------------------------------------------------------
# Cancel
# --------------------------------------------------------------------------


def test_cancel_task_transitions_to_canceled(server):
    task = server.client().cancel_task("t-9")
    assert task.state is TaskState.CANCELED
    body = server.bodies[-1]
    assert body["method"] == "tasks/cancel"
    assert body["params"] == {"id": "t-9"}


def test_cancel_task_accepts_british_spelling(server):
    server.cancel_result = lambda tid, params: {
        "id": tid,
        "status": {"state": "cancelled"},  # British spelling on the wire
    }
    task = server.client().cancel_task("t-9")
    assert task.state is TaskState.CANCELED


# --------------------------------------------------------------------------
# JSON-RPC / HTTP error handling on unary calls
# --------------------------------------------------------------------------


def test_rpc_jsonrpc_error_raises_protocol_error(server):
    server.send_result = lambda tid, params: (_ for _ in ()).throw(
        AssertionError("should not be called")
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/.well-known/agent.json":
            return httpx.Response(200, json=server.card)
        body = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": body["id"],
                "error": {"code": -32602, "message": "bad params", "data": {"x": 1}},
            },
        )

    client = A2AClient(BASE_URL, transport=httpx.MockTransport(handler))
    with pytest.raises(A2AProtocolError) as exc:
        client.send_task(text_message("hi"), task_id="t")
    assert exc.value.code == -32602
    assert exc.value.data == {"x": 1}


def test_rpc_http_error_raises_client_error(server):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/.well-known/agent.json":
            return httpx.Response(200, json=server.card)
        return httpx.Response(500, text="kaboom")

    client = A2AClient(BASE_URL, transport=httpx.MockTransport(handler))
    with pytest.raises(A2AClientError):
        client.send_task(text_message("hi"), task_id="t")


def test_rpc_result_missing_raises_client_error(server):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/.well-known/agent.json":
            return httpx.Response(200, json=server.card)
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1})  # no result

    client = A2AClient(BASE_URL, transport=httpx.MockTransport(handler))
    with pytest.raises(A2AClientError):
        client.get_task("t")
