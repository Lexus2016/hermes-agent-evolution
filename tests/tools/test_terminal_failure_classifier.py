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

    def test_first_timeout_is_retryable(self):
        """A single timeout (consecutive_count=1) is retryable."""
        result = classifier.classify_terminal_failure(
            "sleep 10", 124, "", "", consecutive_count=1
        )
        assert result.category == classifier.FailureCategory.timeout
        assert result.should_retry is True

    def test_second_consecutive_timeout_is_deterministic(self):
        """After 2 consecutive identical timeouts, the failure is
        classified as ``timeout_deterministic`` (non-retryable) so
        the agent gets a distinct signal to change parameters (issue #1091)."""
        result = classifier.classify_terminal_failure(
            "sleep 10", 124, "", "", consecutive_count=2
        )
        assert result.category == classifier.FailureCategory.timeout_deterministic
        assert result.should_retry is False

    def test_high_streak_is_deterministic(self):
        """A high consecutive count also classifies as timeout_deterministic
        (was persistent_error before issue #1091 lowered the threshold)."""
        result = classifier.classify_terminal_failure(
            "sleep 10", 124, "", "", consecutive_count=5
        )
        assert result.category == classifier.FailureCategory.timeout_deterministic
        assert result.should_retry is False

    def test_deterministic_timeout_hint_mentions_parameter_change(self):
        """The hint for timeout_deterministic should tell the agent to
        change command/cwd/timeout/flags, not just 'try again'."""
        result = classifier.classify_terminal_failure(
            "slowcmd", 124, "", "", consecutive_count=3
        )
        assert result.category == classifier.FailureCategory.timeout_deterministic
        assert "Change at least one of" in result.hint
        assert "command" in result.hint.lower()


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


# ---------------------------------------------------------------------------
# Signal-based exit code classification
# ---------------------------------------------------------------------------


class TestClassifySignalExits:
    """Signal-based exits (128+signal) should be classified with a
    human-readable signal name rather than a generic persistent_error."""

    @pytest.mark.parametrize(
        "exit_code, expected_signal",
        [
            (130, "SIGINT"),
            (137, "SIGKILL"),
            (139, "SIGSEGV"),
            (143, "SIGTERM"),
            (134, "SIGABRT"),
            (129, "SIGHUP"),
        ],
    )
    def test_signal_exit_classified_with_name(self, exit_code, expected_signal):
        result = classifier.classify_terminal_failure(
            "somecmd", exit_code, "", ""
        )
        assert result.category == classifier.FailureCategory.persistent_error
        assert result.should_retry is False
        assert expected_signal in result.hint

    def test_unknown_signal_still_classified(self):
        """Exit codes >= 128 that aren't in the known map still get a
        generic 'signal N' hint."""
        result = classifier.classify_terminal_failure(
            "somecmd", 159, "", ""
        )
        assert result.category == classifier.FailureCategory.persistent_error
        assert "signal" in result.hint.lower()

    def test_signal_exit_hint_mentions_investigation(self):
        result = classifier.classify_terminal_failure(
            "somecmd", 137, "", ""
        )
        assert "investigate" in result.hint.lower() or "switch" in result.hint.lower()


# ---------------------------------------------------------------------------
# Error field population for non-zero exits (issue #888)
# ---------------------------------------------------------------------------


class TestErrorFieldPopulation:
    """Non-zero exits should populate the 'error' field with a minimum
    diagnostic instead of leaving it null (issue #888)."""

    def test_unknown_nonzero_exit_populates_error(self, monkeypatch):
        """A non-zero exit that the classifier can't match to a specific
        category should still produce a non-null error field (the
        classifier's persistent_error hint) instead of null."""
        monkeypatch.setenv("TERMINAL_ENV", "local")
        fake = FakeEnvironment([{"output": "some output", "returncode": 42}])
        monkeypatch.setattr(terminal_tool, "_active_environments", {"default": fake})

        result = terminal_tool.terminal_tool("mycommand", task_id="test-streak")
        data = json.loads(result)

        assert data["exit_code"] == 42
        assert data["error"] is not None
        assert data["failure_class"] == "persistent_error"

    def test_unknown_nonzero_no_output_populates_error(self, monkeypatch):
        """A non-zero exit with no output should still produce a non-null
        error field so the agent knows something went wrong."""
        monkeypatch.setenv("TERMINAL_ENV", "local")
        fake = FakeEnvironment([{"output": "", "returncode": 5}])
        monkeypatch.setattr(terminal_tool, "_active_environments", {"default": fake})

        result = terminal_tool.terminal_tool("mycommand", task_id="test-streak")
        data = json.loads(result)

        assert data["exit_code"] == 5
        assert data["error"] is not None
        assert data["failure_class"] == "persistent_error"

    def test_informational_exit_error_is_none(self, monkeypatch):
        """Informational exits (grep=1) should still have error=None
        because exit_note is set."""
        monkeypatch.setenv("TERMINAL_ENV", "local")
        fake = FakeEnvironment([{"output": "", "returncode": 1}])
        monkeypatch.setattr(terminal_tool, "_active_environments", {"default": fake})

        result = terminal_tool.terminal_tool("grep foo bar", task_id="test-streak")
        data = json.loads(result)

        assert data["exit_code"] == 1
        assert data.get("error") is None
        assert data.get("exit_code_meaning") == "No matches found (not an error)"

    def test_classified_failure_error_has_hint(self, monkeypatch):
        """Classified failures (e.g. missing_command) should populate
        the error field with the classification hint."""
        monkeypatch.setenv("TERMINAL_ENV", "local")
        fake = FakeEnvironment([
            {"output": "bash: notreal: command not found", "returncode": 127}
        ])
        monkeypatch.setattr(terminal_tool, "_active_environments", {"default": fake})

        result = terminal_tool.terminal_tool("notreal", task_id="test-streak")
        data = json.loads(result)

        assert data["exit_code"] == 127
        assert data["failure_class"] == "missing_command"
        assert data["error"] is not None
        assert data["error"] == data["suggestion"]

    def test_signal_exit_populates_error_field(self, monkeypatch):
        """Signal-based exits (e.g. 137=SIGKILL) should populate the
        error field with the signal hint."""
        monkeypatch.setenv("TERMINAL_ENV", "local")
        fake = FakeEnvironment([{"output": "", "returncode": 137}])
        monkeypatch.setattr(terminal_tool, "_active_environments", {"default": fake})

        result = terminal_tool.terminal_tool("memoryhog", task_id="test-streak")
        data = json.loads(result)

        assert data["exit_code"] == 137
        assert data["failure_class"] == "persistent_error"
        assert data["error"] is not None
        assert "SIGKILL" in data["error"]

    def test_zero_exit_error_is_none(self, monkeypatch):
        """Successful commands (exit 0) should still have error=None."""
        monkeypatch.setenv("TERMINAL_ENV", "local")
        fake = FakeEnvironment([{"output": "ok", "returncode": 0}])
        monkeypatch.setattr(terminal_tool, "_active_environments", {"default": fake})

        result = terminal_tool.terminal_tool("echo ok", task_id="test-streak")
        data = json.loads(result)

        assert data["exit_code"] == 0
        assert data.get("error") is None
