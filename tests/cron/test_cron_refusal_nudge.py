#!/usr/bin/env python3
"""Tests for cron dispatch refusal-recovery nudge (#2240 Slice B).

Verifies the wiring logic: when a cron job completes as a text-only
refusal (no tool calls), maybe_refusal_nudge fires and the recovery
directive is injected. Recovery (tool calls in re-run) replaces the
original refusal; continued refusal preserves the original.

Mirrors the test pattern from test_delegate_refusal_nudge.py (#2292).
"""

from cron.scheduler import _count_tool_calls


def _make_result(summary: str, messages=None, tool_calls=0):
    """Build a fake run_conversation result dict."""
    msgs = messages or []
    if summary:
        msgs.append({"role": "assistant", "content": summary})
    return {
        "final_response": summary,
        "completed": True,
        "interrupted": False,
        "api_calls": 1,
        "messages": msgs,
        "tool_calls_made": tool_calls,
    }


class TestRefusalNudgeCron:
    """Refusal-recovery nudge fires on cron text-only refusal (#2240 Slice B)."""

    def test_refusal_text_triggers_nudge_detection(self):
        """When cron run completes with refusal text, maybe_refusal_nudge
        would detect it on the messages."""
        from agent.loop_guard import maybe_refusal_nudge

        refusal_result = _make_result(
            "I'm sorry, I can't help with that.",
            messages=[
                {"role": "user", "content": "do the cron task"},
                {
                    "role": "assistant",
                    "content": "I'm sorry, I can't help with that.",
                },
            ],
        )
        # The guard condition: completed AND zero tool calls
        assert refusal_result["completed"]
        assert _count_tool_calls(refusal_result["messages"]) == 0

        # The nudge function detects the refusal
        nudge = maybe_refusal_nudge(
            refusal_result["messages"], already_nudged=False
        )
        assert nudge is not None

    def test_recovery_adopted_when_rerun_has_tool_calls(self):
        """If re-run produces tool calls, the recovered result is adopted."""
        recovery_result = {
            "final_response": "Done! I ran the cron task.",
            "completed": True,
            "messages": [
                {"role": "user", "content": "do the cron task"},
                {"role": "assistant", "content": "I cannot assist."},
                {"role": "user", "content": "[loop-guard] re-check tools"},
                {
                    "role": "assistant",
                    "content": "Running the task now.",
                    "tool_calls": [
                        {
                            "id": "tc1",
                            "function": {
                                "name": "terminal",
                                "arguments": "{}",
                            },
                        }
                    ],
                },
                {"role": "tool", "tool_call_id": "tc1", "content": "done"},
            ],
        }

        # The adopt condition: re-run made tool calls OR no longer a refusal
        tool_calls = _count_tool_calls(recovery_result["messages"])
        assert tool_calls > 0  # → recovery condition met → adopt re-run

    def test_non_refusal_does_not_trigger_nudge(self):
        """A healthy completed result with tool calls skips the nudge path."""
        from agent.loop_guard import maybe_refusal_nudge

        healthy_result = _make_result(
            "I'll help with that. Running the task.",
            messages=[
                {"role": "user", "content": "do the cron task"},
                {
                    "role": "assistant",
                    "content": "I'll help with that. Running the task.",
                },
            ],
        )

        # Guard condition fails: either refusal text is absent or tool calls exist
        nudge = maybe_refusal_nudge(
            healthy_result["messages"], already_nudged=False
        )
        assert nudge is None

    def test_refusal_guard_checks_completed_and_no_tool_calls(self):
        """The cron guard condition: result.completed AND zero tool calls."""
        # Text-only refusal (triggers nudge)
        refusal = _make_result(
            "I can't help.",
            messages=[
                {"role": "user", "content": "x"},
                {"role": "assistant", "content": "I can't help."},
            ],
        )
        assert refusal["completed"]
        assert _count_tool_calls(refusal["messages"]) == 0
        # → guard condition True → nudge fires

        # Tool-using run (does NOT trigger nudge)
        tool_result = _make_result(
            "Done.",
            messages=[
                {"role": "user", "content": "x"},
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {"function": {"name": "terminal", "arguments": "{}"}}
                    ],
                },
                {"role": "tool", "content": "output"},
            ],
        )
        assert tool_result["completed"]
        assert _count_tool_calls(tool_result["messages"]) > 0
        # → guard condition False → nudge skipped

    def test_continued_refusal_preserves_original(self):
        """If re-run still refuses (no tool calls, still refusal text),
        the original result is kept — not adopted."""
        from agent.loop_guard import maybe_refusal_nudge

        rerun_still_refusal = _make_result(
            "I cannot help with that request.",
            messages=[
                {"role": "user", "content": "[loop-guard] take action"},
                {
                    "role": "assistant",
                    "content": "I cannot help with that request.",
                },
            ],
        )

        # Adopt condition: tool_calls > 0 OR not still_refusal
        still_refusal = (
            maybe_refusal_nudge(
                rerun_still_refusal["messages"], already_nudged=True
            )
            is not None
        )
        tool_calls = _count_tool_calls(rerun_still_refusal["messages"])
        # Both conditions fail → do NOT adopt → keep original
        assert still_refusal
        assert tool_calls == 0
        assert not (tool_calls or not still_refusal)  # adopt gate is False


class TestMaybeCronRefusalRecovery:
    """Exercises _maybe_cron_refusal_recovery (#2240 re-implementation).

    The original #2240 Slice B tests verified the adopt-gate LOGIC in
    isolation. These test the REAL helper wired into _run_job_impl: a
    text-only refusal triggers ONE grounded re-run (conversation_history
    passthrough — defect #2), and adoption requires a genuine recovery —
    completed AND not failed (defect #3) AND made tool calls OR no longer
    reads as a refusal.
    """

    class _FakeAgent:
        """Minimal fake exposing the run_conversation method the helper calls."""

        def __init__(self, fn):
            self._fn = fn

        def run_conversation(self, user_message=None, conversation_history=None, **kw):
            return self._fn(user_message, conversation_history)

    def test_refusal_triggers_grounded_rerun_and_adopts(self):
        from cron.scheduler import _maybe_cron_refusal_recovery

        refusal = _make_result(
            "I'm sorry, I can't help with that.",
            messages=[
                {"role": "user", "content": "do the cron task"},
                {
                    "role": "assistant",
                    "content": "I'm sorry, I can't help with that.",
                },
            ],
        )

        calls = []

        def fake_run(user_message, conversation_history):
            calls.append((user_message, conversation_history))
            # Recovery re-run that made a tool call
            return _make_result(
                "Done! I ran the cron task.",
                messages=[
                    {"role": "user", "content": "do the cron task"},
                    {"role": "assistant", "content": "I cannot assist."},
                    {"role": "user", "content": "[loop-guard] re-check tools"},
                    {
                        "role": "assistant",
                        "content": "Running the task now.",
                        "tool_calls": [
                            {"function": {"name": "terminal", "arguments": "{}"}}
                        ],
                    },
                    {"role": "tool", "content": "output"},
                ],
            )

        out = _maybe_cron_refusal_recovery(
            refusal,
            agent=self._FakeAgent(fake_run),
            job_name="job1",
            run_in_context=lambda f: f(),
        )
        assert len(calls) == 1
        user_msg, history = calls[0]
        # Defect #2: the re-run is grounded — original history is passed through
        assert history == refusal["messages"]
        assert _count_tool_calls(out["messages"]) > 0

    def test_failed_rerun_is_not_adopted(self):
        """A re-run that does not complete must not be adopted —
        otherwise a refusal is laundered into a fake completion."""
        from cron.scheduler import _maybe_cron_refusal_recovery

        refusal = _make_result("I can't help.")

        def fake_run(user_message, conversation_history):
            return {"completed": False, "failed": True, "messages": []}

        out = _maybe_cron_refusal_recovery(
            refusal,
            agent=self._FakeAgent(fake_run),
            job_name="job1",
            run_in_context=lambda f: f(),
        )
        # Original preserved; failed re-run NOT adopted
        assert out is refusal