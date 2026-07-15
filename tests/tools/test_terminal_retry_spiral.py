"""Tests for terminal retry-spiral detection (issue #1022).

Covers the identical-failure repeat tracker, the failure signature, the
spiral diagnostic, and the terminal_tool integration that escalates a command
to a ``retry_spiral`` diagnostic only when it produces the SAME failure
back-to-back — so that loops which make progress between runs (edit-then-test,
poll-until-ready) are never flagged.
"""

import json

import pytest

import tools.terminal_failure_classifier as classifier
import tools.terminal_tool as terminal_tool


# ---------------------------------------------------------------------------
# Unit tests: failure signature
# ---------------------------------------------------------------------------


class TestFailureSignature:
    def test_identical_inputs_match(self):
        a = terminal_tool._terminal_failure_signature("make", 1, "boom")
        b = terminal_tool._terminal_failure_signature("make", 1, "boom")
        assert a == b

    def test_differs_on_command(self):
        a = terminal_tool._terminal_failure_signature("make", 1, "boom")
        b = terminal_tool._terminal_failure_signature("pytest", 1, "boom")
        assert a != b

    def test_differs_on_exit_code(self):
        a = terminal_tool._terminal_failure_signature("make", 1, "boom")
        b = terminal_tool._terminal_failure_signature("make", 2, "boom")
        assert a != b

    def test_differs_on_output(self):
        a = terminal_tool._terminal_failure_signature("make", 1, "boom")
        b = terminal_tool._terminal_failure_signature("make", 1, "kaboom")
        assert a != b

    def test_command_whitespace_normalized(self):
        a = terminal_tool._terminal_failure_signature("make ", 1, "x")
        b = terminal_tool._terminal_failure_signature("  make", 1, "x")
        assert a == b

    def test_signature_is_fixed_size_hash(self):
        # Bounded regardless of output size, so per-task retained state stays
        # small even for huge command output.
        small = terminal_tool._terminal_failure_signature("make", 1, "x")
        huge = terminal_tool._terminal_failure_signature("make", 1, "y" * 500_000)
        assert len(small) == len(huge) == 40
        assert small != huge


# ---------------------------------------------------------------------------
# Unit tests: identical-failure repeat tracker
# ---------------------------------------------------------------------------


class TestFailureRepeatTracker:
    def _reset(self, task_id):
        terminal_tool._reset_terminal_failure_repeats(task_id)

    def test_first_failure_returns_one(self):
        self._reset("t")
        assert terminal_tool._note_terminal_failure("t", "make", 1, "boom") == 1

    def test_identical_failure_increments(self):
        self._reset("t")
        assert terminal_tool._note_terminal_failure("t", "make", 1, "boom") == 1
        assert terminal_tool._note_terminal_failure("t", "make", 1, "boom") == 2
        assert terminal_tool._note_terminal_failure("t", "make", 1, "boom") == 3

    def test_different_command_resets(self):
        self._reset("t")
        terminal_tool._note_terminal_failure("t", "make", 1, "boom")
        terminal_tool._note_terminal_failure("t", "make", 1, "boom")
        assert terminal_tool._note_terminal_failure("t", "pytest", 1, "boom") == 1

    def test_changed_output_resets(self):
        # The core anti-false-positive property: a changed result (progress)
        # resets the counter even for the same command.
        self._reset("t")
        assert terminal_tool._note_terminal_failure("t", "pytest", 1, "3 failed") == 1
        assert terminal_tool._note_terminal_failure("t", "pytest", 1, "2 failed") == 1
        assert terminal_tool._note_terminal_failure("t", "pytest", 1, "1 failed") == 1

    def test_reset_clears_counter(self):
        self._reset("t")
        terminal_tool._note_terminal_failure("t", "make", 1, "boom")
        terminal_tool._reset_terminal_failure_repeats("t")
        assert terminal_tool.get_terminal_failure_repeat("t") == 0

    def test_tasks_are_isolated(self):
        self._reset("a")
        self._reset("b")
        terminal_tool._note_terminal_failure("a", "make", 1, "boom")
        terminal_tool._note_terminal_failure("a", "make", 1, "boom")
        assert terminal_tool._note_terminal_failure("b", "make", 1, "boom") == 1
        assert terminal_tool.get_terminal_failure_repeat("a") == 2


# ---------------------------------------------------------------------------
# Unit tests: spiral-break diagnostic
# ---------------------------------------------------------------------------


class TestSpiralBreakDiagnostic:
    def test_mentions_counts_and_alternatives(self):
        msg = classifier.spiral_break_diagnostic("make build", repeat_count=4, budget=3)
        assert "4" in msg
        assert "3" in msg
        assert "make build" in msg
        assert any(t in msg for t in ("read_file", "execute_code", "web_search"))

    def test_long_command_is_truncated(self):
        long_cmd = "echo " + "x" * 500
        msg = classifier.spiral_break_diagnostic(long_cmd, repeat_count=4, budget=3)
        assert "..." in msg
        assert "x" * 200 not in msg


# ---------------------------------------------------------------------------
# Integration: terminal_tool force-break on identical spiral
# ---------------------------------------------------------------------------


class FixedEnvironment:
    """Environment double that returns the same response for every call."""

    def __init__(self, response):
        self._response = response
        self.calls = []
        self.env = {}
        self.cwd = ""

    def execute(self, command, **kwargs):
        self.calls.append((command, kwargs))
        return self._response


class VaryingEnvironment:
    """Environment double whose output changes every call (simulates progress)."""

    def __init__(self, returncode=1):
        self._returncode = returncode
        self.calls = []
        self.env = {}
        self.cwd = ""

    def execute(self, command, **kwargs):
        n = len(self.calls)
        self.calls.append((command, kwargs))
        return {"output": f"attempt {n}", "returncode": self._returncode}


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch):
    for key in ("spiral-test", "default"):
        terminal_tool._reset_terminal_streak(key)
        terminal_tool._reset_terminal_failure_repeats(key)
    # Deterministic budget regardless of the machine's config.yaml.
    monkeypatch.setattr(terminal_tool, "_get_max_command_repeats", lambda: 3)
    yield
    for key in ("spiral-test", "default"):
        terminal_tool._reset_terminal_streak(key)
        terminal_tool._reset_terminal_failure_repeats(key)


class TestTerminalSpiralBreak:
    def test_identical_failures_escalate_after_budget(self, monkeypatch):
        monkeypatch.setenv("TERMINAL_ENV", "local")
        fake = FixedEnvironment({"output": "boom", "returncode": 1})
        monkeypatch.setattr(terminal_tool, "_active_environments", {"default": fake})

        # Budget 3: three identical failures return the normal classification,
        # the fourth identical failure is escalated to retry_spiral.
        for _ in range(3):
            data = json.loads(
                terminal_tool.terminal_tool("false", task_id="spiral-test")
            )
            assert data["failure_class"] == "persistent_error"

        data = json.loads(terminal_tool.terminal_tool("false", task_id="spiral-test"))
        assert data["failure_class"] == "retry_spiral"
        assert data["should_retry"] is False
        assert data["failure_repeat_count"] == 4
        assert len(fake.calls) == 4

    def test_escalation_preserves_streak_and_recommendation(self, monkeypatch):
        # The escalated result must keep the real output/exit code and the
        # existing streak/recommendation enrichment — it only sharpens the
        # error text, it does not drop context.
        monkeypatch.setenv("TERMINAL_ENV", "local")
        fake = FixedEnvironment({"output": "boom", "returncode": 1})
        monkeypatch.setattr(terminal_tool, "_active_environments", {"default": fake})

        data = {}
        for _ in range(4):
            data = json.loads(
                terminal_tool.terminal_tool("false", task_id="spiral-test")
            )
        assert data["failure_class"] == "retry_spiral"
        assert data["exit_code"] == 1
        assert data["output"] == "boom"
        assert "terminal_streak" in data
        assert "recommendation" in data  # streak is high enough by the 4th call

    def test_changing_output_never_breaks(self, monkeypatch):
        # Gemini review case: an edit-then-test or poll-until-ready loop re-runs
        # the identical command but the OUTPUT changes each time (progress).
        # The guard must never fire here.
        monkeypatch.setenv("TERMINAL_ENV", "local")
        fake = VaryingEnvironment(returncode=1)
        monkeypatch.setattr(terminal_tool, "_active_environments", {"default": fake})

        for _ in range(8):
            data = json.loads(
                terminal_tool.terminal_tool("pytest -q", task_id="spiral-test")
            )
            assert data["failure_class"] != "retry_spiral"
        assert len(fake.calls) == 8

    def test_success_resets_the_spiral_counter(self, monkeypatch):
        monkeypatch.setenv("TERMINAL_ENV", "local")
        fail_env = FixedEnvironment({"output": "boom", "returncode": 1})
        monkeypatch.setattr(
            terminal_tool, "_active_environments", {"default": fail_env}
        )
        for _ in range(2):
            terminal_tool.terminal_tool("flaky", task_id="spiral-test")
        assert terminal_tool.get_terminal_failure_repeat("spiral-test") == 2

        ok_env = FixedEnvironment({"output": "ok", "returncode": 0})
        monkeypatch.setattr(terminal_tool, "_active_environments", {"default": ok_env})
        data = json.loads(terminal_tool.terminal_tool("flaky", task_id="spiral-test"))
        assert data["exit_code"] == 0
        assert terminal_tool.get_terminal_failure_repeat("spiral-test") == 0

    def test_different_command_does_not_trip_the_guard(self, monkeypatch):
        monkeypatch.setenv("TERMINAL_ENV", "local")
        fake = FixedEnvironment({"output": "boom", "returncode": 1})
        monkeypatch.setattr(terminal_tool, "_active_environments", {"default": fake})

        for cmd in ("cmd_a", "cmd_b", "cmd_a", "cmd_b", "cmd_a", "cmd_b"):
            data = json.loads(terminal_tool.terminal_tool(cmd, task_id="spiral-test"))
            assert data["failure_class"] == "persistent_error"
        assert len(fake.calls) == 6
