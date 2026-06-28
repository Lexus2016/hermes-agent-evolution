"""Tests for tools.terminal_failure_classifier and terminal_tool integration."""

import json
from unittest.mock import MagicMock

import pytest

import tools.terminal_failure_classifier as classifier
import tools.terminal_tool as terminal_tool


# ---------------------------------------------------------------------------
# Unit tests for the classifier
# ---------------------------------------------------------------------------


class TestClassifyMissingCommand:
    def test_exit_code_127(self):
        result = classifier.classify_terminal_failure(
            "notarealcommand123", 127, "", "bash: notarealcommand123: command not found"
        )
        assert result.category == classifier.FailureCategory.missing_command
        assert result.should_retry is False

    def test_stderr_pattern(self):
        result = classifier.classify_terminal_failure(
            "foo", 1, "", "foo: command not found"
        )
        assert result.category == classifier.FailureCategory.missing_command


class TestClassifyPermissionDenied:
    def test_exit_code_126(self):
        result = classifier.classify_terminal_failure(
            "/root/secret", 126, "", "bash: /root/secret: Permission denied"
        )
        assert result.category == classifier.FailureCategory.permission_denied
        assert result.should_retry is False

    def test_stderr_pattern(self):
        result = classifier.classify_terminal_failure(
            "cat /etc/shadow", 1, "", "Permission denied"
        )
        assert result.category == classifier.FailureCategory.permission_denied


class TestClassifyTimeout:
    def test_exit_code_124(self):
        result = classifier.classify_terminal_failure("sleep 10", 124, "", "")
        assert result.category == classifier.FailureCategory.timeout
        assert result.should_retry is True

    def test_high_streak_becomes_persistent(self):
        result = classifier.classify_terminal_failure(
            "sleep 10", 124, "", "", consecutive_count=5
        )
        assert result.category == classifier.FailureCategory.persistent_error
        assert result.should_retry is False


class TestClassifyRetryableTransient:
    def test_network_unreachable(self):
        result = classifier.classify_terminal_failure(
            "curl http://example.test", 7, "", "curl: (6) Could not resolve host"
        )
        assert result.category == classifier.FailureCategory.retryable_transient
        assert result.should_retry is True

    def test_high_streak_downgrades_to_persistent(self):
        result = classifier.classify_terminal_failure(
            "curl http://example.test",
            7,
            "",
            "Connection refused",
            consecutive_count=4,
        )
        assert result.category == classifier.FailureCategory.persistent_error
        assert result.should_retry is False


class TestClassifyPersistentError:
    def test_generic_nonzero(self):
        result = classifier.classify_terminal_failure(
            "python -c 'raise RuntimeError(\"boom\")'", 1, "", "Traceback"
        )
        assert result.category == classifier.FailureCategory.persistent_error
        assert result.should_retry is False


class TestClassifyInformationalCommands:
    @pytest.mark.parametrize(
        "command, exit_code",
        [
            ("grep foo bar", 1),
            ("diff a b", 1),
            ("git diff", 1),
            ("test -f missing", 1),
        ],
    )
    def test_informational_exits_are_unknown(self, command, exit_code):
        result = classifier.classify_terminal_failure(command, exit_code, "", "")
        assert result.category == classifier.FailureCategory.unknown
        assert result.should_retry is False


class TestStreakRecommendation:
    def test_low_streak_no_recommendation(self):
        assert classifier.streak_recommendation(1) is None
        assert classifier.streak_recommendation(2) is None

    def test_high_streak_returns_recommendation(self):
        rec = classifier.streak_recommendation(4)
        assert rec is not None
        assert "4" in rec
        assert "read_file" in rec


# ---------------------------------------------------------------------------
# Integration tests for terminal_tool foreground failure handling
# ---------------------------------------------------------------------------


class FakeEnvironment:
    """Minimal environment double for foreground execution tests."""

    def __init__(self, responses):
        self._responses = list(responses)
        self._index = 0
        self.calls = []
        self.env = {}
        self.cwd = ""

    def execute(self, command, **kwargs):
        self.calls.append((command, kwargs))
        # If only one response was provided, repeat it for every call so
        # retries see the same failure (and single-exception tests exhaust
        # all retries consistently).
        if len(self._responses) == 1:
            response = self._responses[0]
        else:
            response = self._responses[self._index]
            self._index += 1
        if isinstance(response, Exception):
            raise response
        return response


@pytest.fixture(autouse=True)
def _clean_streaks(monkeypatch):
    terminal_tool._reset_terminal_streak("test-streak")
    terminal_tool._reset_terminal_streak("default")
    yield
    terminal_tool._reset_terminal_streak("test-streak")
    terminal_tool._reset_terminal_streak("default")


class TestTerminalToolForegroundFailures:
    def test_missing_command_stops_without_retry(self, monkeypatch):
        monkeypatch.setenv("TERMINAL_ENV", "local")
        fake = FakeEnvironment([
            {"output": "bash: notreal: command not found", "returncode": 127}
        ])
        monkeypatch.setattr(terminal_tool, "_active_environments", {"default": fake})

        result = terminal_tool.terminal_tool("notreal", task_id="test-streak")
        data = json.loads(result)

        assert data["exit_code"] == 127
        assert data["failure_class"] == "missing_command"
        assert data["should_retry"] is False
        assert "suggestion" in data
        assert len(fake.calls) == 1
        assert fake.calls[0][0] == "notreal"
        assert "timeout" in fake.calls[0][1]
        assert "cwd" in fake.calls[0][1]

    def test_permission_denied_stops_without_retry(self, monkeypatch):
        monkeypatch.setenv("TERMINAL_ENV", "local")
        fake = FakeEnvironment([{"output": "Permission denied", "returncode": 126}])
        monkeypatch.setattr(terminal_tool, "_active_environments", {"default": fake})

        result = terminal_tool.terminal_tool("cat /root/secret", task_id="test-streak")
        data = json.loads(result)

        assert data["exit_code"] == 126
        assert data["failure_class"] == "permission_denied"
        assert data["should_retry"] is False
        assert len(fake.calls) == 1
        assert fake.calls[0][0] == "cat /root/secret"

    def test_transient_timeout_retries_then_stops(self, monkeypatch):
        monkeypatch.setenv("TERMINAL_ENV", "local")
        monkeypatch.setattr(terminal_tool.time, "sleep", lambda _s: None)
        # Four consecutive timeouts: initial + 3 retries should exhaust the loop.
        fake = FakeEnvironment([
            {"output": "", "returncode": 124},
            {"output": "", "returncode": 124},
            {"output": "", "returncode": 124},
            {"output": "", "returncode": 124},
        ])
        monkeypatch.setattr(terminal_tool, "_active_environments", {"default": fake})
        monkeypatch.setattr(terminal_tool, "_last_activity", {"default": 0})

        result = terminal_tool.terminal_tool(
            "slowcmd", timeout=1, task_id="test-streak"
        )
        data = json.loads(result)

        assert data["exit_code"] == 124
        assert data["failure_class"] == "timeout"
        assert (
            data["should_retry"] is False
        )  # retries exhausted; caller should switch strategy
        assert len(fake.calls) == 4

    def test_successful_command_resets_streak(self, monkeypatch):
        monkeypatch.setenv("TERMINAL_ENV", "local")
        fake = FakeEnvironment([{"output": "ok", "returncode": 0}])
        monkeypatch.setattr(terminal_tool, "_active_environments", {"default": fake})

        # Prime the streak with a prior failure.
        terminal_tool._increment_terminal_streak("test-streak")
        terminal_tool._increment_terminal_streak("test-streak")
        assert terminal_tool.get_terminal_streak("test-streak") == 2

        result = terminal_tool.terminal_tool("echo ok", task_id="test-streak")
        data = json.loads(result)

        assert data["exit_code"] == 0
        assert terminal_tool.get_terminal_streak("test-streak") == 0

    def test_retryable_transient_retries_until_exhausted(self, monkeypatch):
        monkeypatch.setenv("TERMINAL_ENV", "local")
        monkeypatch.setattr(terminal_tool.time, "sleep", lambda _s: None)
        fake = FakeEnvironment([
            {"output": "connection refused", "returncode": 1},
            {"output": "connection refused", "returncode": 1},
            {"output": "connection refused", "returncode": 1},
            {"output": "connection refused", "returncode": 1},
        ])
        monkeypatch.setattr(terminal_tool, "_active_environments", {"default": fake})

        result = terminal_tool.terminal_tool("curl http://test", task_id="test-streak")
        data = json.loads(result)

        assert data["exit_code"] == 1
        assert data["failure_class"] == "retryable_transient"
        assert data["should_retry"] is False  # retries exhausted
        assert len(fake.calls) == 4

    def test_persistent_error_includes_streak_recommendation(self, monkeypatch):
        monkeypatch.setenv("TERMINAL_ENV", "local")
        # Bump the streak high enough to trigger the recommendation.
        for _ in range(4):
            terminal_tool._increment_terminal_streak("test-streak")

        fake = FakeEnvironment([{"output": "boom", "returncode": 1}])
        monkeypatch.setattr(terminal_tool, "_active_environments", {"default": fake})

        result = terminal_tool.terminal_tool(
            "python -c 'raise SystemExit(1)'", task_id="test-streak"
        )
        data = json.loads(result)

        assert data["failure_class"] == "persistent_error"
        assert data["should_retry"] is False
        assert "terminal_streak" in data
        assert "recommendation" in data
        assert "read_file" in data["recommendation"]

    def test_execution_exception_classified(self, monkeypatch):
        monkeypatch.setenv("TERMINAL_ENV", "local")
        monkeypatch.setattr(terminal_tool.time, "sleep", lambda _s: None)
        fake = FakeEnvironment([Exception("connection refused by host")])
        monkeypatch.setattr(terminal_tool, "_active_environments", {"default": fake})

        result = terminal_tool.terminal_tool("curl http://test", task_id="test-streak")
        data = json.loads(result)

        assert data["exit_code"] == -1
        assert data["failure_class"] == "retryable_transient"
        assert data["should_retry"] is False  # retries exhausted

    def test_execution_exception_timeout_returns_timeout_class(self, monkeypatch):
        monkeypatch.setenv("TERMINAL_ENV", "local")
        monkeypatch.setattr(terminal_tool.time, "sleep", lambda _s: None)
        fake = FakeEnvironment([Exception("command timed out after 1 seconds")])
        monkeypatch.setattr(terminal_tool, "_active_environments", {"default": fake})

        result = terminal_tool.terminal_tool(
            "sleep 99", timeout=1, task_id="test-streak"
        )
        data = json.loads(result)

        assert data["exit_code"] == 124
        assert data["failure_class"] == "timeout"
        assert data["should_retry"] is True

    def test_informational_grep_exit_does_not_classify_as_failure(self, monkeypatch):
        monkeypatch.setenv("TERMINAL_ENV", "local")
        fake = FakeEnvironment([{"output": "", "returncode": 1}])
        monkeypatch.setattr(terminal_tool, "_active_environments", {"default": fake})

        result = terminal_tool.terminal_tool("grep foo bar", task_id="test-streak")
        data = json.loads(result)

        assert data["exit_code"] == 1
        assert data.get("failure_class") is None
        assert data.get("exit_code_meaning") == "No matches found (not an error)"


class TestTerminalStreakHelpers:
    def test_streak_counter_increments_and_resets(self):
        terminal_tool._reset_terminal_streak("s1")
        assert terminal_tool.get_terminal_streak("s1") == 0
        assert terminal_tool._increment_terminal_streak("s1") == 1
        assert terminal_tool._increment_terminal_streak("s1") == 2
        terminal_tool._reset_terminal_streak("s1")
        assert terminal_tool.get_terminal_streak("s1") == 0

    def test_streak_default_key(self):
        terminal_tool._reset_terminal_streak(None)
        assert terminal_tool._increment_terminal_streak(None) == 1
        assert terminal_tool.get_terminal_streak() == 1
        terminal_tool._reset_terminal_streak(None)
