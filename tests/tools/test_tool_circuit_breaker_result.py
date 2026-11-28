"""The retry-spiral note must never corrupt a machine-parsed tool result (#3241).

``handle_function_call`` appends a ``[CIRCUIT_BREAKER]`` paragraph to a text result so the
model stops retrying. ``execute_code`` helpers parse that same result as JSON inside the
script, so a tripped breaker used to surface as ``json.decoder.JSONDecodeError: Extra data``
instead of the tool's answer.
"""

from __future__ import annotations

import json

from tools.code_execution_rpc import _handle_rpc_request
from tools.tool_circuit_breaker import (
    CIRCUIT_BREAKER_MARKER,
    append_diagnostic,
    strip_diagnostic,
)

_PAYLOAD = json.dumps({"output": "", "exit_code": -1, "error": "", "status": "pending_approval"})
_EVENT = {
    "circuit_breaker_tripped": True,
    "consecutive_failures": 3,
    "budget": 5,
    "failure_class": "permission_denied",
    "tool_name": "terminal",
    "strategy_recommendation": "Verify prerequisites before retrying.",
}


def test_append_diagnostic_round_trips_to_the_original_payload():
    noted = append_diagnostic(_PAYLOAD, _EVENT)
    assert CIRCUIT_BREAKER_MARKER in noted and noted.startswith(_PAYLOAD)
    assert strip_diagnostic(noted) == _PAYLOAD
    assert json.loads(strip_diagnostic(noted))["status"] == "pending_approval"


def test_append_diagnostic_is_idempotent_and_survives_a_missing_key():
    event = {key: value for key, value in _EVENT.items() if key != "strategy_recommendation"}
    noted = append_diagnostic(_PAYLOAD, event, label="Suggested action")
    assert "Suggested action:" in noted  # no KeyError for the caller-supplied label
    assert append_diagnostic(noted, _EVENT) == noted


def test_strip_diagnostic_leaves_plain_and_clean_results_alone():
    assert strip_diagnostic("no json here") == "no json here"
    assert strip_diagnostic(None) is None
    assert strip_diagnostic(_PAYLOAD) == _PAYLOAD


def test_rpc_request_returns_valid_json_when_the_breaker_tripped():
    noted = append_diagnostic(_PAYLOAD, _EVENT)
    log: list = []
    counter = [0]
    response = _handle_rpc_request(
        {"tool": "terminal", "args": {"command": "rm -rf /tmp/x"}, "token": "t"},
        allowed_tools=frozenset({"terminal"}), tool_call_counter=counter, max_tool_calls=5,
        dispatch=lambda name, args: noted, tool_call_log=log, call_start=0.0, where="sandbox",
    )
    assert response == _PAYLOAD
    assert json.loads(response)["status"] == "pending_approval"
    assert counter == [1] and log and "error" not in log[0]
