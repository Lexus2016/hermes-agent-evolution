"""A2A (Agent2Agent) task lifecycle *client*.

Slice 2 of #748 (issue #880). Complements the discovery view from #879
(``hermes_cli/a2a.py``, which advertises *this* Hermes as an Agent Card):
this module lets Hermes act as an A2A *caller* -- discover a remote agent's
Agent Card, send it a task, observe the task's state transitions over
Server-Sent Events (SSE), and cancel a running task.

It rides Hermes' existing outbound-HTTP seam (``httpx`` -- already a core
dependency and the transport used throughout ``hermes_cli/web_server.py``),
so it adds **no** new dependency and registers **no** new core tool. An A2A
client is a capability at the edge, driven by the ``skills/a2a`` skill.

Wire protocol: A2A speaks JSON-RPC 2.0 over HTTP. The task methods used here
are ``tasks/send`` (run one turn), ``tasks/sendSubscribe`` (same, but the
response is an SSE stream of ``TaskStatusUpdateEvent`` / ``TaskArtifactUpdate``
frames), ``tasks/get`` (poll) and ``tasks/cancel``. Task state flows through
``submitted -> working -> (input-required) -> completed`` or is short-circuited
to ``canceled`` / ``failed``. See https://google.github.io/A2A/ .
"""

from __future__ import annotations

import enum
import itertools
import json
import logging
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, Iterator, List, Optional, Tuple
from urllib.parse import urljoin

import httpx

from hermes_cli.a2a import AgentSkill

logger = logging.getLogger(__name__)

WELL_KNOWN_PATH = "/.well-known/agent.json"
_DEFAULT_TIMEOUT = 30.0
# SSE streams are long-lived; bound a *silent* server so subscribe() cannot
# hang forever waiting for the next frame, while still allowing slow work.
_DEFAULT_STREAM_READ_TIMEOUT = 120.0
# Hard cap on frames consumed from one subscription -- defends against a
# server that streams forever without ever marking a frame ``final``.
_MAX_STREAM_EVENTS = 10_000
# Cap on ``data:`` lines buffered for a *single* SSE event. A conformant
# server terminates every event with a blank line; a peer that streams endless
# ``data:`` lines without one would otherwise grow this buffer without bound
# (each line resets the read timeout), so cap it and abort.
_MAX_DATA_LINES_PER_EVENT = 100_000


class A2AClientError(RuntimeError):
    """Base class for A2A client failures (transport, HTTP, decode)."""


class A2AProtocolError(A2AClientError):
    """A JSON-RPC ``error`` object was returned by the remote agent."""

    def __init__(self, code: int, message: str, data: Any = None) -> None:
        self.code = code
        self.data = data
        super().__init__(f"A2A JSON-RPC error {code}: {message}")


class TaskState(str, enum.Enum):
    """A2A task lifecycle states.

    The five states called out by #880 -- ``submitted``, ``working``,
    ``input-required``, ``completed``, ``canceled`` (cancelled) -- plus
    ``failed`` and an ``unknown`` sentinel so an unexpected wire value never
    crashes the state machine.
    """

    SUBMITTED = "submitted"
    WORKING = "working"
    INPUT_REQUIRED = "input-required"
    COMPLETED = "completed"
    CANCELED = "canceled"
    FAILED = "failed"
    UNKNOWN = "unknown"

    @classmethod
    def from_wire(cls, value: Any) -> "TaskState":
        """Coerce a wire value to a member.

        Accepts both the A2A spelling ``canceled`` and the British
        ``cancelled`` for the cancelled state; anything unrecognised maps to
        :attr:`UNKNOWN` rather than raising.
        """
        text = str(value or "").strip().lower()
        if text in ("cancelled", "canceled"):
            return cls.CANCELED
        try:
            return cls(text)
        except ValueError:
            return cls.UNKNOWN

    @property
    def is_terminal(self) -> bool:
        return self in _TERMINAL_STATES


_TERMINAL_STATES = frozenset({
    TaskState.COMPLETED,
    TaskState.CANCELED,
    TaskState.FAILED,
})


@dataclass
class TaskStatus:
    """The ``status`` block of an A2A task (state + optional agent message)."""

    state: TaskState
    message: Optional[Dict[str, Any]] = None
    timestamp: Optional[str] = None

    @classmethod
    def from_dict(cls, data: Optional[Dict[str, Any]]) -> "TaskStatus":
        data = data or {}
        return cls(
            state=TaskState.from_wire(data.get("state")),
            message=data.get("message"),
            timestamp=data.get("timestamp"),
        )


@dataclass
class Task:
    """A snapshot of a remote A2A task (result of tasks/send|get|cancel)."""

    id: str
    status: TaskStatus
    session_id: Optional[str] = None
    artifacts: List[Dict[str, Any]] = field(default_factory=list)
    history: List[Dict[str, Any]] = field(default_factory=list)
    raw: Dict[str, Any] = field(default_factory=dict)

    @property
    def state(self) -> TaskState:
        return self.status.state

    @classmethod
    def from_result(cls, result: Dict[str, Any]) -> "Task":
        return cls(
            id=str(result.get("id", "")),
            status=TaskStatus.from_dict(result.get("status")),
            session_id=result.get("sessionId"),
            artifacts=list(result.get("artifacts") or []),
            history=list(result.get("history") or []),
            raw=result,
        )


@dataclass
class TaskStatusUpdate:
    """One SSE ``TaskStatusUpdateEvent`` frame observed during subscribe()."""

    task_id: str
    status: TaskStatus
    final: bool = False

    @property
    def state(self) -> TaskState:
        return self.status.state


@dataclass
class DiscoveredAgent:
    """Parsed remote Agent Card plus the resolved JSON-RPC endpoint."""

    name: str
    endpoint: str
    card: Dict[str, Any]
    skills: List[AgentSkill] = field(default_factory=list)

    @property
    def supports_streaming(self) -> bool:
        caps = self.card.get("capabilities") or {}
        return bool(caps.get("streaming"))


def text_message(text: str, role: str = "user") -> Dict[str, Any]:
    """Build an A2A ``Message`` with a single text part."""
    return {"role": role, "parts": [{"type": "text", "text": text}]}


def _new_task_id() -> str:
    return uuid.uuid4().hex


class A2AClient:
    """Client for the A2A task lifecycle against one remote agent.

    ``base_url`` is the remote agent's origin (e.g. ``https://peer.example``).
    The JSON-RPC endpoint is taken from the discovered Agent Card's ``url``;
    :meth:`discover` runs lazily on first use if not called explicitly, so a
    bare ``send_task`` still performs discover -> invoke.

    ``transport`` is injectable so tests drive a fully in-memory mock A2A
    server via :class:`httpx.MockTransport` -- no real network.
    """

    def __init__(
        self,
        base_url: str,
        *,
        auth_token: Optional[str] = None,
        timeout: float = _DEFAULT_TIMEOUT,
        stream_read_timeout: float = _DEFAULT_STREAM_READ_TIMEOUT,
        transport: Optional[httpx.BaseTransport] = None,
        max_stream_events: int = _MAX_STREAM_EVENTS,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.auth_token = auth_token
        self.timeout = timeout
        self.stream_read_timeout = stream_read_timeout
        self._transport = transport
        self._max_stream_events = max_stream_events
        self._endpoint: Optional[str] = None
        self._ids = itertools.count(1)

    # -- discovery --------------------------------------------------------

    def discover(self) -> DiscoveredAgent:
        """Fetch and parse the remote ``/.well-known/agent.json`` card."""
        url = self.base_url + WELL_KNOWN_PATH
        with self._client() as client:
            card = self._decode_json(self._send(client, "GET", url))
        if not isinstance(card, dict):
            raise A2AClientError(f"Agent Card at {url} was not a JSON object")
        raw_endpoint = str(card.get("url") or "").strip()
        # A relative endpoint is resolved against the discovery origin; an
        # absolute one is left untouched.
        self._endpoint = (
            urljoin(self.base_url + "/", raw_endpoint)
            if raw_endpoint
            else self.base_url
        )
        skills = [
            AgentSkill(
                id=str(s.get("id", "")),
                name=str(s.get("name", "")),
                description=str(s.get("description", "")),
                tags=list(s.get("tags") or []),
            )
            for s in (card.get("skills") or [])
            if isinstance(s, dict)
        ]
        return DiscoveredAgent(
            name=str(card.get("name", "")),
            endpoint=self._endpoint,
            card=card,
            skills=skills,
        )

    @property
    def endpoint(self) -> str:
        """The JSON-RPC endpoint, discovering it lazily on first access."""
        if self._endpoint is None:
            self.discover()
        assert self._endpoint is not None
        return self._endpoint

    # -- task lifecycle ---------------------------------------------------

    def send_task(
        self,
        message: Dict[str, Any],
        *,
        task_id: Optional[str] = None,
        session_id: Optional[str] = None,
    ) -> Task:
        """Send one task turn (``tasks/send``) and return the resulting Task."""
        params: Dict[str, Any] = {"id": task_id or _new_task_id(), "message": message}
        if session_id:
            params["sessionId"] = session_id
        return Task.from_result(self._rpc("tasks/send", params))

    def get_task(self, task_id: str, *, history_length: Optional[int] = None) -> Task:
        """Poll a task's current state (``tasks/get``)."""
        params: Dict[str, Any] = {"id": task_id}
        if history_length is not None:
            params["historyLength"] = history_length
        return Task.from_result(self._rpc("tasks/get", params))

    def cancel_task(self, task_id: str) -> Task:
        """Request cancellation of a running task (``tasks/cancel``)."""
        return Task.from_result(self._rpc("tasks/cancel", {"id": task_id}))

    def subscribe(
        self,
        message: Dict[str, Any],
        *,
        task_id: Optional[str] = None,
        session_id: Optional[str] = None,
    ) -> Iterator[TaskStatusUpdate]:
        """Send a task and yield its status transitions from the SSE stream.

        This is a generator. The underlying HTTP stream is closed when it is
        exhausted, when the caller stops early (``GeneratorExit`` via
        ``.close()`` or ``break``), or on error -- the ``with`` blocks below
        guarantee it. Iteration stops after a frame marked ``final`` or one
        carrying a terminal state, and is hard-capped at ``max_stream_events``
        frames, so a misbehaving server cannot hang the caller indefinitely.
        """
        endpoint = self.endpoint  # lazy-discover before opening the stream
        params: Dict[str, Any] = {"id": task_id or _new_task_id(), "message": message}
        if session_id:
            params["sessionId"] = session_id
        payload = self._jsonrpc("tasks/sendSubscribe", params)
        # Overall connect budget from ``timeout``; per-frame read budget is the
        # (larger) stream read timeout so slow work does not trip a false hang.
        timeout = httpx.Timeout(self.timeout, read=self.stream_read_timeout)
        with self._client(timeout=timeout) as client:
            with client.stream(
                "POST",
                endpoint,
                json=payload,
                headers={"Accept": "text/event-stream"},
            ) as response:
                try:
                    response.raise_for_status()
                except httpx.HTTPStatusError as exc:
                    raise A2AClientError(
                        f"A2A subscribe -> HTTP {exc.response.status_code}"
                    ) from exc
                lines = response.iter_lines()
                for _event, data in _iter_sse(lines, self._max_stream_events):
                    update = self._parse_stream_frame(data)
                    if update is None:
                        continue
                    yield update
                    if update.final or update.state.is_terminal:
                        return

    # -- internals --------------------------------------------------------

    def _jsonrpc(self, method: str, params: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "jsonrpc": "2.0",
            "id": next(self._ids),
            "method": method,
            "params": params,
        }

    def _rpc(self, method: str, params: Dict[str, Any]) -> Dict[str, Any]:
        payload = self._jsonrpc(method, params)
        with self._client() as client:
            response = self._send(client, "POST", self.endpoint, json_body=payload)
        return self._extract_result(self._decode_json(response))

    def _extract_result(self, body: Any) -> Dict[str, Any]:
        if not isinstance(body, dict):
            raise A2AClientError("A2A response was not a JSON-RPC object")
        error = body.get("error")
        if error:
            raise A2AProtocolError(
                int(error.get("code", 0)),
                str(error.get("message", "")),
                error.get("data"),
            )
        result = body.get("result")
        if not isinstance(result, dict):
            raise A2AClientError("A2A JSON-RPC response missing 'result'")
        return result

    def _parse_stream_frame(self, data: str) -> Optional[TaskStatusUpdate]:
        try:
            frame = json.loads(data)
        except json.JSONDecodeError:
            logger.debug("A2A: skipping non-JSON SSE frame: %r", data[:120])
            return None
        if not isinstance(frame, dict):
            return None
        error = frame.get("error")
        if error:
            raise A2AProtocolError(
                int(error.get("code", 0)),
                str(error.get("message", "")),
                error.get("data"),
            )
        result = frame.get("result")
        # Only TaskStatusUpdateEvent frames carry a ``status``; artifact-update
        # and other frame types have no state to observe, so skip them.
        if not isinstance(result, dict) or "status" not in result:
            return None
        return TaskStatusUpdate(
            task_id=str(result.get("id", "")),
            status=TaskStatus.from_dict(result.get("status")),
            final=bool(result.get("final")),
        )

    def _client(self, *, timeout: Optional[httpx.Timeout] = None) -> httpx.Client:
        return httpx.Client(
            transport=self._transport,
            timeout=timeout or httpx.Timeout(self.timeout),
            headers=self._headers(),
        )

    def _headers(self) -> Dict[str, str]:
        headers = {"User-Agent": "Hermes-Agent/a2a-client"}
        if self.auth_token:
            headers["Authorization"] = f"Bearer {self.auth_token}"
        return headers

    def _send(
        self,
        client: httpx.Client,
        method: str,
        url: str,
        *,
        json_body: Any = None,
    ) -> httpx.Response:
        try:
            response = client.request(method, url, json=json_body)
            response.raise_for_status()
            return response
        except httpx.HTTPStatusError as exc:
            raise A2AClientError(
                f"A2A {method} {url} -> HTTP {exc.response.status_code}"
            ) from exc
        except httpx.HTTPError as exc:
            raise A2AClientError(f"A2A {method} {url} failed: {exc}") from exc

    @staticmethod
    def _decode_json(response: httpx.Response) -> Any:
        try:
            return response.json()
        except (json.JSONDecodeError, ValueError) as exc:
            raise A2AClientError("A2A response was not valid JSON") from exc


def _iter_sse(
    lines: Iterator[str], max_events: int
) -> Iterator[Tuple[Optional[str], str]]:
    """Parse an SSE line stream into ``(event, data)`` tuples.

    ``lines`` is an iterator of newline-stripped lines (httpx
    ``iter_lines()``). Multiple ``data:`` lines within one event are joined
    with newlines per the SSE spec; comment lines (``:`` prefix) are ignored;
    a value's single leading space after the colon is stripped. Emits at most
    ``max_events`` frames, then stops.
    """
    data: List[str] = []
    event: Optional[str] = None
    emitted = 0
    for raw in lines:
        line = raw.rstrip("\r")
        if line == "":
            if data:
                yield event, "\n".join(data)
                emitted += 1
                data, event = [], None
                if emitted >= max_events:
                    return
            continue
        if line.startswith(":"):
            continue  # comment / keep-alive
        field_name, sep, value = line.partition(":")
        if sep and value.startswith(" "):
            value = value[1:]
        if field_name == "data":
            data.append(value)
            if len(data) > _MAX_DATA_LINES_PER_EVENT:
                raise A2AClientError(
                    "A2A SSE event exceeded the data-line buffer cap "
                    f"({_MAX_DATA_LINES_PER_EVENT}); aborting stream"
                )
        elif field_name == "event":
            event = value
    if data:  # flush a final event not terminated by a blank line
        yield event, "\n".join(data)
