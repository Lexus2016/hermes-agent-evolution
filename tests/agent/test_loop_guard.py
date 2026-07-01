"""Tests for agent/loop_guard.py — advisory loop / repeated-failure detection.

Mutating tools (terminal, write_file, etc.) get LOWER thresholds because
fixation on them is more costly (#432). Idempotent tools (read_file, etc.)
use higher thresholds. Tests use ``terminal`` (mutating, threshold=4) and
``read_file`` (idempotent, threshold=8) to exercise both paths.
"""

import json

from agent.loop_guard import (
    CRON_LOOP_GUARD_HARD_STOP_THRESHOLD,
    current_run_signature,
    maybe_nudge,
    should_cron_hard_stop,
)


def _asst(tool, args="{}", call_id="c"):
    return {
        "role": "assistant",
        "tool_calls": [{"id": call_id, "function": {"name": tool, "arguments": args}}],
    }


def _result(content, call_id="c"):
    return {"role": "tool", "tool_call_id": call_id, "content": content}


def _run(tool, n, *, result="ok"):
    """n consecutive single-tool turns, each with a (non-failing) result."""
    msgs = [{"role": "user", "content": "do the thing"}]
    for i in range(n):
        cid = f"c{i}"
        msgs.append(_asst(tool, call_id=cid))
        msgs.append(_result(result, call_id=cid))
    return msgs


class TestRepeatTrigger:
    def test_mutating_below_threshold_is_quiet(self):
        # terminal is mutating (repeat_threshold=4) — 3 calls should be quiet.
        assert maybe_nudge(_run("terminal", 3)) is None

    def test_mutating_at_threshold_nudges(self):
        n = maybe_nudge(_run("terminal", 4))
        assert n is not None and "terminal" in n and "loop-guard" in n

    def test_idempotent_below_threshold_is_quiet(self):
        # read_file is idempotent (repeat_threshold=8) — 7 calls should be quiet.
        assert maybe_nudge(_run("read_file", 7)) is None

    def test_idempotent_at_threshold_nudges(self):
        n = maybe_nudge(_run("read_file", 8))
        assert n is not None and "read_file" in n and "loop-guard" in n

    def test_signature_counts_the_run(self):
        assert current_run_signature(_run("read_file", 4)) == ("read_file", 4)

    def test_no_tools_no_nudge(self):
        assert maybe_nudge([{"role": "user", "content": "hi"}]) is None


class TestFailureTrigger:
    def test_mutating_two_failures_nudge(self):
        # terminal is mutating (fail_threshold=2) — 2 failures trigger.
        msgs = _run("terminal", 2, result="error: build step blew up")
        n = maybe_nudge(msgs)
        assert n is not None and "failed 2 times" in n

    def test_mutating_one_failure_not_enough(self):
        assert maybe_nudge(_run("terminal", 1, result="error: transient blip")) is None

    def test_idempotent_three_failures_still_quiet(self):
        # read_file is idempotent (fail_threshold=4) — 3 failures is below threshold.
        assert maybe_nudge(_run("read_file", 3, result="error: not found")) is None

    def test_idempotent_four_failures_nudge(self):
        msgs = _run("read_file", 4, result="error: not found")
        n = maybe_nudge(msgs)
        assert n is not None and "failed 4 times" in n

    def test_exit_code_marker_counts_as_failure(self):
        msgs = _run("execute_code", 2, result="process finished, exit code: 1")
        assert maybe_nudge(msgs) is not None

    def test_mcp_unreachable_failures(self):
        msgs = _run(
            "mcp_tqmemory_health", 3, result="server unreachable: ClosedResourceError"
        )
        n = maybe_nudge(msgs)
        # mcp_tqmemory_health is not in mutating/idempotent sets, so 'unknown'
        # category uses the safer (lower) default -> mutating thresholds.
        # fail_threshold=2 for unknown, so 3 failures trigger.
        assert n is not None


class TestNonRetryableTrigger:
    """#231 — DETERMINISTIC failure classes (timeout/permission/missing_command/
    limit) reproduce on retry, so two in a row trip a hard stop below the generic
    strike threshold.
    """

    def test_two_permission_denials_stop_hard(self):
        n = maybe_nudge(_run("terminal", 2, result="permission denied"))
        assert n is not None and "non-retryable" in n and "permission" in n

    def test_two_timeouts_stop_hard(self):
        n = maybe_nudge(
            _run(
                "terminal", 2, result="failure-class=timeout — The operation timed out"
            )
        )
        assert n is not None and "non-retryable" in n and "timeout" in n

    def test_single_deterministic_failure_is_quiet(self):
        assert maybe_nudge(_run("terminal", 1, result="permission denied")) is None

    def test_mixed_deterministic_classes_fall_through_to_generic_fail(self):
        # A permission then a timeout are different classes — the deterministic
        # counter only fires on the SAME class repeating. But the generic fail
        # threshold for mutating tools is 2, so 2 mixed failures STILL trigger
        # via the fail path (not the non-retryable path).
        msgs = [{"role": "user", "content": "go"}]
        msgs += [_asst("terminal", call_id="c0"), _result("permission denied", "c0")]
        msgs += [_asst("terminal", call_id="c1"), _result("connection timed out", "c1")]
        n = maybe_nudge(msgs)
        # Falls through to generic fail path: 2 failures >= mutating fail_threshold=2
        assert n is not None and "failed 2 times" in n


class TestEscalatedInterrupt:
    """#432 — mono-tool spirals beyond the repeat threshold get an escalated
    FORCED INTERRUPT message requiring the agent to summarize progress.
    """

    def test_mutating_escalated_at_threshold(self):
        # terminal mutating: repeat=4, escalate=8. At 8 calls, expect escalated.
        msgs = _run("terminal", 8)
        n = maybe_nudge(msgs)
        assert n is not None and "ESCALATED INTERRUPT" in n

    def test_mutating_escalated_above_threshold(self):
        msgs = _run("terminal", 10)
        n = maybe_nudge(msgs)
        assert n is not None and "ESCALATED INTERRUPT" in n

    def test_idempotent_escalated_at_threshold(self):
        # read_file idempotent: repeat=8, escalate=15. At 15 calls, expect escalated.
        msgs = _run("read_file", 15)
        n = maybe_nudge(msgs)
        assert n is not None and "ESCALATED INTERRUPT" in n

    def test_idempotent_below_escalate_is_regular_nudge(self):
        # read_file idempotent: repeat=8, escalate=15. At 10 calls, regular nudge.
        msgs = _run("read_file", 10)
        n = maybe_nudge(msgs)
        assert n is not None and "ESCALATED INTERRUPT" not in n

    def test_mutating_below_escalate_is_regular_nudge(self):
        # terminal mutating: repeat=4, escalate=8. At 6 calls, regular nudge.
        msgs = _run("terminal", 6)
        n = maybe_nudge(msgs)
        assert n is not None and "ESCALATED INTERRUPT" not in n

    def test_unknown_tool_uses_mutating_thresholds(self):
        # mcp tools not in either set use the safer default (mutating thresholds).
        msgs = _run("mcp_custom_query", 10)
        n = maybe_nudge(msgs)
        # repeat=4 for unknown (mutating default), escalate=8. At 10, escalated.
        assert n is not None and "unknown" in n and "ESCALATED INTERRUPT" in n

    def test_spiral_intensity_appears_at_high_counts(self):
        # terminal mutating: repeat=4, escalate=8. At 10 calls, spiral-intensity >= 2.
        msgs = _run("terminal", 10)
        n = maybe_nudge(msgs)
        assert n is not None and "spiral-intensity" in n


class TestRunBoundaries:
    def test_tool_change_breaks_the_run(self):
        # 5x terminal then 1x read_file at the tail -> only the read_file run (len 1)
        msgs = _run("terminal", 5) + _run("read_file", 1)[1:]
        assert current_run_signature(msgs) == ("read_file", 1)
        assert maybe_nudge(msgs) is None

    def test_text_reply_breaks_the_run(self):
        msgs = _run("terminal", 8)
        msgs.append({"role": "assistant", "content": "Here is my summary."})
        assert maybe_nudge(msgs) is None  # the run was broken by a text turn

    def test_multi_tool_turn_breaks_the_run(self):
        msgs = _run("terminal", 8)
        msgs.append({
            "role": "assistant",
            "tool_calls": [
                {"id": "m1", "function": {"name": "read_file", "arguments": "{}"}},
                {"id": "m2", "function": {"name": "terminal", "arguments": "{}"}},
            ],
        })
        assert maybe_nudge(msgs) is None  # varied multi-tool work, not a spiral


class TestSameQueryShortCircuit:
    """#467 — a spiral-prone idempotent tool (web_search / web_extract /
    search_files) called repeatedly with the SAME input arguments (the query)
    is a loop even though each call technically 'succeeds'.

    The identity of a call MUST be derived from the assistant tool-call INPUT
    arguments (``tool_calls[].function.arguments``), NOT from the tool result:
    results do not carry the input args, and web_search / web_extract outputs
    are XML-wrapped in ``<untrusted_tool_result>`` so they never parse back into
    arguments. Reading identity from the result left this short-circuit inert.
    """

    def _web_run(self, queries, *, tool="web_search", result="3 relevant hits"):
        """One assistant turn per query (single web-tool call), each with a
        NON-failing result. ``queries`` items may be JSON-string OR dict args."""
        msgs = [{"role": "user", "content": "research the topic"}]
        for i, q in enumerate(queries):
            cid = f"c{i}"
            args = q if isinstance(q, str) else json.dumps(q)
            msgs.append(_asst(tool, args=args, call_id=cid))
            msgs.append(_result(result, call_id=cid))
        return msgs

    def test_same_query_fires_short_circuit(self):
        # 4 identical queries: >= short-circuit threshold (4) and < idempotent
        # repeat threshold (8), so ONLY the #467 same-query path can fire here.
        msgs = self._web_run(['{"query": "best cat food"}'] * 4)
        n = maybe_nudge(msgs)
        assert n is not None
        assert "SAME arguments" in n and "web_search" in n and "loop-guard" in n

    def test_same_query_dict_args_fires(self):
        # function.arguments may already be a parsed dict, not a JSON string.
        # Build messages directly so a real dict reaches the call args and the
        # dict branch of the identity hash is exercised (not the string path).
        msgs = [{"role": "user", "content": "research the topic"}]
        for i in range(4):
            cid = f"c{i}"
            msgs.append(_asst("web_search", args={"query": "best cat food"}, call_id=cid))
            msgs.append(_result("3 relevant hits", call_id=cid))
        n = maybe_nudge(msgs)
        assert n is not None and "SAME arguments" in n

    def test_same_query_arg_key_order_insensitive(self):
        # Canonical identity: differently-ordered keys are the SAME query.
        msgs = self._web_run([
            '{"query": "cats", "limit": 5}',
            '{"limit": 5, "query": "cats"}',
            '{"query": "cats", "limit": 5}',
            '{"limit": 5, "query": "cats"}',
        ])
        n = maybe_nudge(msgs)
        assert n is not None and "SAME arguments" in n

    def test_web_extract_same_query_fires(self):
        msgs = self._web_run(['{"url": "https://x.test"}'] * 4, tool="web_extract")
        n = maybe_nudge(msgs)
        assert n is not None and "SAME arguments" in n and "web_extract" in n

    def test_different_queries_no_short_circuit(self):
        # 4 DIFFERENT queries: no same-query short-circuit, and below the
        # idempotent repeat threshold (8) -> no nudge at all. No false positive.
        msgs = self._web_run([
            '{"query": "cat food"}',
            '{"query": "dog food"}',
            '{"query": "fish tanks"}',
            '{"query": "bird cages"}',
        ])
        assert maybe_nudge(msgs) is None

    def test_different_queries_still_spiral_at_repeat_threshold(self):
        # 8 DIFFERENT web_search queries: the same-query short-circuit must NOT
        # fire (varied args), but the GENERIC mono-tool spiral detection still
        # triggers at the idempotent repeat threshold (8). Proves the fix leaves
        # spiral detection intact and does not over-trigger on varied queries.
        msgs = self._web_run([f'{{"query": "topic {i}"}}' for i in range(8)])
        n = maybe_nudge(msgs)
        assert n is not None
        assert "SAME arguments" not in n  # not the #467 short-circuit
        assert "web_search" in n  # the generic spiral nudge still fires


class TestShouldCronHardStop:
    """#624: unattended cron turns get real enforcement, not just advisory
    text — interactive surfaces (a human is present) are never affected."""

    def test_below_threshold_never_stops(self):
        assert should_cron_hard_stop("cron", CRON_LOOP_GUARD_HARD_STOP_THRESHOLD - 1) is False

    def test_at_threshold_stops(self):
        assert should_cron_hard_stop("cron", CRON_LOOP_GUARD_HARD_STOP_THRESHOLD) is True

    def test_above_threshold_stops(self):
        assert should_cron_hard_stop("cron", CRON_LOOP_GUARD_HARD_STOP_THRESHOLD + 5) is True

    def test_zero_warnings_never_stops(self):
        assert should_cron_hard_stop("cron", 0) is False

    def test_non_cron_platforms_never_stop_regardless_of_count(self):
        huge_count = CRON_LOOP_GUARD_HARD_STOP_THRESHOLD + 100
        for platform in ("cli", "telegram", "discord", "gateway", None, ""):
            assert should_cron_hard_stop(platform, huge_count) is False, platform
