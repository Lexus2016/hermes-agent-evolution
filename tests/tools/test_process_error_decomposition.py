"""Tests for process tool error decomposition (#2621).

The ``process`` tool's ``other`` error bucket was the largest undecomposed
failure mode — opaque errors with no reason subclass and no recovery hint
drove deep retry spirals. ``_classify_process_error`` maps each error text to
a concrete category + recovery hint so the agent can act instead of
blind-retrying, mirroring the decomposition done for ``tool_call``/``memory``/
``tool_describe``.
"""

import json
import pytest
from unittest.mock import patch

from tools.process_registry import _classify_process_error, _handle_process


class TestClassifyProcessError:
    """Unit tests for the error classifier."""

    def test_session_not_found(self):
        cls = _classify_process_error("No process with ID 'abc123'")
        assert cls["error_category"] == "session-not-found"
        assert "action='list'" in cls["recovery_hint"]

    def test_session_not_found_no_processes(self):
        cls = _classify_process_error(
            "No process with ID 'abc123'. No processes are currently registered."
        )
        assert cls["error_category"] == "session-not-found"

    def test_invalid_action(self):
        cls = _classify_process_error("Unknown process action: 'status'")
        assert cls["error_category"] == "invalid-action"
        assert "list" in cls["recovery_hint"]

    def test_session_id_required(self):
        cls = _classify_process_error("session_id is required for poll")
        assert cls["error_category"] == "session-id-required"

    def test_process_exited(self):
        cls = _classify_process_error("Process has already finished")
        assert cls["error_category"] == "process-exited"
        assert "action='log'" in cls["recovery_hint"]

    def test_poll_timeout(self):
        cls = _classify_process_error("Wait window of 30s elapsed — timed out")
        assert cls["error_category"] == "poll-timeout"

    def test_other_fallback(self):
        cls = _classify_process_error("Some unexpected opaque error")
        assert cls["error_category"] == "other"
        assert "action='list'" in cls["recovery_hint"]

    def test_empty_message_falls_back_to_other(self):
        cls = _classify_process_error("")
        assert cls["error_category"] == "other"


class TestHandleProcessErrorClassification:
    """The handler's error returns carry the classification fields."""

    def test_invalid_action_error_includes_category(self):
        result = _handle_process({"action": "status"}, task_id="test")
        data = json.loads(result)
        assert data["error_category"] == "invalid-action"
        assert "recovery_hint" in data

    def test_session_id_required_includes_category(self):
        with (
            patch("tools.process_registry.process_registry") as mock_reg,
            patch("tools.approval.get_current_session_key") as mock_key,
        ):
            mock_reg.list_sessions.return_value = []
            mock_key.return_value = ""
            result = _handle_process({"action": "poll"}, task_id="test")
            data = json.loads(result)
            assert data["error_category"] == "session-id-required"
            assert "recovery_hint" in data

    def test_stale_session_id_includes_category(self):
        with (
            patch("tools.process_registry.process_registry") as mock_reg,
            patch("tools.approval.get_current_session_key") as mock_key,
        ):
            mock_reg.get.return_value = None
            mock_reg.list_sessions.return_value = [
                {"session_id": "proc_a", "status": "running"}
            ]
            mock_key.return_value = ""
            result = _handle_process(
                {"action": "poll", "session_id": "stale_id"},
                task_id="test",
            )
            data = json.loads(result)
            assert data["error_category"] == "session-not-found"
            assert "recovery_hint" in data
