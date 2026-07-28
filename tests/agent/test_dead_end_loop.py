# -*- coding: utf-8 -*-
"""Tests for dead-end loop detection (issue #1312)."""

from __future__ import annotations

from agent.loop_guard import detect_dead_end_loop


def _make_tool_call(name: str, args: str, call_id: str = "1") -> dict:
    """Build a minimal assistant message with one tool call."""
    return {
        "role": "assistant",
        "tool_calls": [
            {
                "id": call_id,
                "type": "function",
                "function": {"name": name, "arguments": args},
            }
        ],
    }


def _make_tool_result(call_id: str, content: str = "ok") -> dict:
    return {"role": "tool", "tool_call_id": call_id, "content": content}


class TestDetectDeadEndLoop:
    def test_no_messages_returns_none(self):
        assert detect_dead_end_loop([]) is None

    def test_single_call_returns_none(self):
        """One call is not a loop."""
        msgs = [_make_tool_call("terminal", '{"command": "ls"}', "1")]
        assert detect_dead_end_loop(msgs) is None

    def test_identical_call_at_threshold_triggers(self):
        """4 identical (tool, args) re-issues = dead-end.

        The threshold matches _SHORT_CIRCUIT_REPEAT_THRESHOLD /
        _MUTATING_REPEAT_THRESHOLD; below it, loop_guard deliberately stays
        quiet (a retry after a transient failure is normal).
        """
        msgs = [
            _make_tool_call("terminal", '{"command": "pytest"}', "1"),
            _make_tool_result("1", "FAIL"),
            _make_tool_call("patch", '{"path": "x.py"}', "2"),
            _make_tool_result("2", "ok"),
            _make_tool_call("terminal", '{"command": "pytest"}', "3"),
            _make_tool_result("3", "FAIL"),
            _make_tool_call("patch", '{"path": "y.py"}', "4"),
            _make_tool_result("4", "ok"),
            _make_tool_call("terminal", '{"command": "pytest"}', "5"),
            _make_tool_result("5", "FAIL"),
            _make_tool_call("patch", '{"path": "z.py"}', "6"),
            _make_tool_result("6", "ok"),
            _make_tool_call("terminal", '{"command": "pytest"}', "7"),
            _make_tool_result("7", "FAIL"),
        ]
        result = detect_dead_end_loop(msgs)
        assert result is not None
        assert "DEAD-END DETECTED" in result
        assert "terminal" in result
        assert "what_changed_since_last_attempt" in result
        assert "why_expect_different_outcome" in result

    def test_below_threshold_is_quiet(self):
        """Three identical re-issues are not yet a dead end."""
        msgs = [
            _make_tool_call("terminal", '{"command": "pytest"}', "1"),
            _make_tool_result("1", "FAIL"),
            _make_tool_call("patch", '{"path": "x.py"}', "2"),
            _make_tool_result("2", "ok"),
            _make_tool_call("terminal", '{"command": "pytest"}', "3"),
            _make_tool_result("3", "FAIL"),
            _make_tool_call("patch", '{"path": "y.py"}', "4"),
            _make_tool_result("4", "ok"),
            _make_tool_call("terminal", '{"command": "pytest"}', "5"),
            _make_tool_result("5", "FAIL"),
        ]
        assert detect_dead_end_loop(msgs) is None

    def test_moved_on_to_another_tool_is_quiet(self):
        """A repeated call the agent has already abandoned is not a live loop."""
        msgs = []
        for i in range(1, 6):
            msgs.append(_make_tool_call("terminal", '{"command": "pytest"}', str(i)))
            msgs.append(_make_tool_result(str(i), "FAIL"))
        msgs.append(_make_tool_call("read_file", '{"path": "x.py"}', "99"))
        msgs.append(_make_tool_result("99", "ok"))
        assert detect_dead_end_loop(msgs) is None

    def test_different_args_no_trigger(self):
        """Different arguments = not a dead-end (agent is trying variations)."""
        msgs = [
            _make_tool_call("terminal", '{"command": "pytest tests/a.py"}', "1"),
            _make_tool_result("1", "FAIL"),
            _make_tool_call("terminal", '{"command": "pytest tests/b.py"}', "2"),
            _make_tool_result("2", "FAIL"),
        ]
        assert detect_dead_end_loop(msgs) is None

    def test_four_identical_calls_report_the_count(self):
        """Four identical calls with intervening edits = dead-end."""
        msgs = [
            _make_tool_call("terminal", '{"command": "npm test"}', "1"),
            _make_tool_result("1", "FAIL"),
            _make_tool_call("patch", '{"path": "a.js"}', "2"),
            _make_tool_result("2", "ok"),
            _make_tool_call("terminal", '{"command": "npm test"}', "3"),
            _make_tool_result("3", "FAIL"),
            _make_tool_call("patch", '{"path": "b.js"}', "4"),
            _make_tool_result("4", "ok"),
            _make_tool_call("terminal", '{"command": "npm test"}', "5"),
            _make_tool_result("5", "FAIL"),
            _make_tool_call("patch", '{"path": "c.js"}', "6"),
            _make_tool_result("6", "ok"),
            _make_tool_call("terminal", '{"command": "npm test"}', "7"),
            _make_tool_result("7", "FAIL"),
        ]
        result = detect_dead_end_loop(msgs)
        assert result is not None
        assert "4 times" in result

    def test_nudge_contains_structured_justification_prompt(self):
        """The nudge must ask for structured justification (#1312 success criteria)."""
        msgs = []
        for i in range(1, 5):
            msgs.append(_make_tool_call("execute_code", '{"code": "print(1)"}', str(i)))
            msgs.append(_make_tool_result(str(i), "1"))
        result = detect_dead_end_loop(msgs)
        assert result is not None
        assert "strategy" in result.lower() or "different approach" in result.lower()

    def test_window_respected(self):
        """Calls outside the window should not be counted."""
        # Fill with 60 non-matching messages, then the identical run
        msgs = []
        for i in range(30):
            cid = str(i)
            msgs.append(_make_tool_call("read_file", f'{{"path": "f{i}.py"}}', cid))
            msgs.append(_make_tool_result(cid))
        for i in range(100, 104):
            msgs.append(_make_tool_call("terminal", '{"command": "ls"}', str(i)))
            msgs.append(_make_tool_result(str(i)))
        # With default window=60, the four identical "ls" calls at the end
        # ARE within the last 60 messages and should trigger.
        result = detect_dead_end_loop(msgs)
        assert result is not None


class TestAlternatingCallsAreStillDeadEnds:
    """Two stuck calls alternating is a dead end, not variety.

    Regression for a defect found in review: the check ranked the window by
    `most_common` and then required THAT winner to be the live one. With two
    signatures re-issued four times each, the counter returns the first-inserted
    on a tie while the latest turn is the other — they never matched, and a
    textbook dead end went unreported. The gate is now on the live signature's
    OWN count.
    """

    @staticmethod
    def _alternating(n: int) -> list[dict]:
        msgs: list[dict] = []
        for i in range(n):
            msgs.append(_make_tool_call("terminal", '{"command": "pytest"}', f"a{i}"))
            msgs.append(_make_tool_result(f"a{i}", "FAIL"))
            msgs.append(_make_tool_call("terminal", '{"command": "ruff check"}', f"b{i}"))
            msgs.append(_make_tool_result(f"b{i}", "FAIL"))
        return msgs

    def test_alternating_pair_at_threshold_fires(self):
        result = detect_dead_end_loop(self._alternating(4))
        assert result is not None
        assert "4 times" in result

    def test_alternating_pair_below_threshold_is_quiet(self):
        assert detect_dead_end_loop(self._alternating(3)) is None

    def test_fires_on_the_live_signature_not_the_most_common(self):
        """The nudge must describe the call being re-issued now."""
        msgs = self._alternating(3)
        # one extra `ruff` so it is BOTH the live call and above threshold,
        # while `pytest` sits one below.
        msgs.append(_make_tool_call("terminal", '{"command": "ruff check"}', "z"))
        msgs.append(_make_tool_result("z", "FAIL"))
        result = detect_dead_end_loop(msgs)
        assert result is not None
        assert "4 times" in result


class TestSemanticNudgeOrdering:
    """Both cross-turn detectors must stay reachable through `maybe_nudge`.

    `_semantic_loop_nudge` chains them with `or`. Test-iteration fires at 3 and
    dead-end at 4, so running test-iteration first short-circuited before
    dead-end could ever run for a repeated test command — silently removing
    #1312's structured-justification nudge from exactly the `test -> edit ->
    test` loop the issue was filed about. These tests pin the composed path, not
    just the individual detectors: nothing in the suite exercised `maybe_nudge`
    for this input class before.
    """

    from agent.loop_guard import maybe_nudge as _maybe_nudge_fn

    # Wrapped in staticmethod so Python does not bind it as a method and pass
    # `self` as the messages argument.
    _maybe_nudge = staticmethod(_maybe_nudge_fn)

    @staticmethod
    def _identical_test_loop(n: int) -> list[dict]:
        """Same test command every time, a DIFFERENT file edited between runs.

        The edits must vary: an identical `patch` repeated n times is itself a
        dead end, and would be the signature reported instead of the test.
        """
        msgs: list[dict] = []
        for i in range(n):
            msgs.append(_make_tool_call("patch", f'{{"path": "f{i}.py"}}', f"p{i}"))
            msgs.append(_make_tool_result(f"p{i}", "ok"))
            msgs.append(_make_tool_call("terminal", '{"command": "pytest"}', f"t{i}"))
            msgs.append(_make_tool_result(f"t{i}", "1 failed, 2 passed in 0.31s"))
        return msgs

    @staticmethod
    def _varying_test_loop(n: int) -> list[dict]:
        """A DIFFERENT test command each run, with an edit in between.

        The edits matter: back-to-back failing `terminal` calls trip the
        consecutive-failure branch higher up the ladder, which is correct
        behaviour but means the semantic fallback is never reached.
        """
        msgs: list[dict] = []
        for i in range(n):
            msgs.append(_make_tool_call("patch", f'{{"path": "f{i}.py"}}', f"p{i}"))
            msgs.append(_make_tool_result(f"p{i}", "ok"))
            msgs.append(
                _make_tool_call("terminal", f'{{"command": "pytest tests/t{i}.py"}}', f"t{i}")
            )
            msgs.append(_make_tool_result(f"t{i}", "1 failed in 0.2s"))
        return msgs

    def test_identical_test_command_reaches_the_dead_end_nudge(self):
        """Same arguments four deep is a dead end, not merely a test loop."""
        nudge = self._maybe_nudge(self._identical_test_loop(4))
        assert nudge is not None
        assert "DEAD-END DETECTED" in nudge
        assert "what_changed_since_last_attempt" in nudge

    def test_varying_test_command_still_gets_test_scoping(self):
        """Different arguments each run is not a dead end — the test-iteration
        detector must still be reachable behind it."""
        nudge = self._maybe_nudge(self._varying_test_loop(3))
        assert nudge is not None
        assert "SEMANTIC TEST LOOP" in nudge

    def test_test_iteration_quiet_below_its_own_threshold(self):
        """The detector had no coverage at all; pin its quiet contract too.

        Asserted on the detector directly: below its threshold `maybe_nudge`
        legitimately returns other, more specific nudges, so the composed path
        cannot isolate this contract.
        """
        from agent.loop_guard import detect_test_iteration_loop

        assert detect_test_iteration_loop(self._varying_test_loop(2)) is None
        assert detect_test_iteration_loop(self._varying_test_loop(3)) is not None
