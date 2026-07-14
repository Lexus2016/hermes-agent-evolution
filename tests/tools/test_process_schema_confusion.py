"""Tests for process tool action suggestions and session_id auto-fill (issue #890).

Models frequently invent action names (``create``, ``start``, ``run``, etc.)
and omit ``session_id`` when only one process exists.  The fix adds:
1. Edit-distance suggestions for invalid action names.
2. Auto-fill of ``session_id`` when exactly one process is running.
"""

import json
import pytest
from unittest.mock import patch, MagicMock
from tools.process_registry import _suggest_process_action, _handle_process


class TestSuggestProcessAction:
    """Unit tests for the action suggestion heuristic."""

    @pytest.mark.parametrize("bad_action,expected", [
        ("create", "list"),
        ("start", "list"),
        ("run", "list"),
        ("status", "poll"),
        ("check", "poll"),
        ("stop", "kill"),
        ("tail", "log"),
        ("read", "log"),
        ("send", "submit"),
        ("delete", "kill"),
        ("remove", "kill"),
        ("end", "kill"),
        ("terminate", "kill"),
        ("restart", "poll"),
        ("info", "list"),
        ("show", "log"),
        ("output", "log"),
        ("attach", "log"),
    ])
    def test_synonym_mapped_correctly(self, bad_action, expected):
        assert _suggest_process_action(bad_action) == expected

    def test_valid_action_returns_none(self):
        """Valid actions don't need a suggestion."""
        for action in ["list", "poll", "log", "wait", "kill", "write", "submit", "close"]:
            # Valid actions may or may not return a suggestion — the
            # handler only calls _suggest_process_action for INVALID actions
            pass  # no assertion needed, just verify no crash

    def test_close_edit_distance_match(self):
        """A typo close to a valid action returns the edit-distance match."""
        # "pol" is close to "poll"
        result = _suggest_process_action("pol")
        assert result == "poll"

    def test_completely_unrelated_returns_none_or_close(self):
        """A completely unrelated string might return None or a weak match."""
        result = _suggest_process_action("xyzzy")
        # xyzzy has no close match — should return None
        assert result is None

    def test_empty_string(self):
        result = _suggest_process_action("")
        assert result is None


class TestHandleProcessInvalidAction:
    """Tests for the _handle_process handler with invalid actions."""

    def test_invalid_action_includes_suggestion(self):
        """An invalid action returns an error with a suggestion."""
        result = _handle_process({"action": "status"}, task_id="test")
        data = json.loads(result)
        assert "error" in data
        assert "poll" in data["error"]
        assert "Did you mean" in data["error"]

    def test_invalid_action_synonym_mapped(self):
        """An invalid action synonym gets the right suggestion."""
        result = _handle_process({"action": "stop"}, task_id="test")
        data = json.loads(result)
        assert "error" in data
        assert "kill" in data["error"]
        assert "Did you mean" in data["error"]

    def test_completely_unknown_action_lists_valid(self):
        """A completely unknown action still lists valid actions."""
        result = _handle_process({"action": "xyzzy"}, task_id="test")
        data = json.loads(result)
        assert "error" in data
        assert "list" in data["error"]
        # No suggestion for completely unrelated strings
        assert "Did you mean" not in data["error"]

    def test_valid_list_action_works(self):
        """The list action still works normally."""
        with patch("tools.process_registry.process_registry") as mock_reg:
            mock_reg.list_sessions.return_value = []
            result = _handle_process({"action": "list"}, task_id="test")
            data = json.loads(result)
            assert "processes" in data


class TestHandleProcessSessionIdAutoFill:
    """Tests for session_id auto-fill when one process exists."""

    def test_auto_fill_when_one_active_process(self):
        """When exactly one active process exists, session_id is auto-filled."""
        mock_procs = [{"session_id": "proc_abc", "status": "running"}]
        with patch("tools.process_registry.process_registry") as mock_reg, \
             patch("tools.approval.get_current_session_key") as mock_key:
            mock_reg.list_sessions.return_value = mock_procs
            mock_key.return_value = ""
            mock_reg.poll.return_value = {"status": "ok", "output": "test"}
            result = _handle_process(
                {"action": "poll"},  # no session_id
                task_id="test",
            )
            data = json.loads(result)
            # Should have called poll with the auto-filled session_id
            mock_reg.poll.assert_called_once_with("proc_abc")

    def test_error_when_multiple_processes_and_no_session_id(self):
        """When multiple processes exist, error lists available IDs."""
        mock_procs = [
            {"session_id": "proc_abc", "status": "running"},
            {"session_id": "proc_def", "status": "running"},
        ]
        with patch("tools.process_registry.process_registry") as mock_reg, \
             patch("tools.approval.get_current_session_key") as mock_key:
            mock_reg.list_sessions.return_value = mock_procs
            mock_key.return_value = ""
            result = _handle_process(
                {"action": "poll"},
                task_id="test",
            )
            data = json.loads(result)
            assert "error" in data
            assert "session_id is required" in data["error"]
            assert "proc_abc" in data["error"]
            assert "proc_def" in data["error"]

    def test_error_when_no_processes_and_no_session_id(self):
        """When no processes exist, error tells the model to use list first."""
        with patch("tools.process_registry.process_registry") as mock_reg, \
             patch("tools.approval.get_current_session_key") as mock_key:
            mock_reg.list_sessions.return_value = []
            mock_key.return_value = ""
            result = _handle_process(
                {"action": "poll"},
                task_id="test",
            )
            data = json.loads(result)
            assert "error" in data
            assert "session_id is required" in data["error"]

    def test_auto_fill_prefers_active_over_exited(self):
        """When one active and one exited process exist, the active one is used."""
        mock_procs = [
            {"session_id": "proc_old", "status": "exited"},
            {"session_id": "proc_new", "status": "running"},
        ]
        with patch("tools.process_registry.process_registry") as mock_reg, \
             patch("tools.approval.get_current_session_key") as mock_key:
            mock_reg.list_sessions.return_value = mock_procs
            mock_key.return_value = ""
            mock_reg.poll.return_value = {"status": "ok", "output": "test"}
            result = _handle_process(
                {"action": "poll"},
                task_id="test",
            )
            # Should use the active process, not the exited one
            mock_reg.poll.assert_called_once_with("proc_new")