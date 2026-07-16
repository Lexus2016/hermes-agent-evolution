"""Tests for agent/loop_guard.py — advisory loop / repeated-failure detection.

Mutating tools (terminal, write_file, etc.) get LOWER thresholds because
fixation on them is more costly (#432). Idempotent tools (read_file, etc.)
use higher thresholds. Tests use ``terminal`` (mutating, threshold=4) and
``read_file`` (idempotent, threshold=8) to exercise both paths.
"""

import json

from agent.loop_guard import (
    CRON_LOOP_GUARD_HARD_STOP_THRESHOLD,
    INTERACTIVE_LOOP_GUARD_HARD_STOP_THRESHOLD,
    current_run_signature,
    maybe_nudge,
    maybe_refusal_nudge,
    run_warrants_cron_hard_stop,
    should_cron_hard_stop,
    should_interactive_hard_stop,
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
        # read_file is idempotent (repeat_threshold=8). 7 reads of DIFFERENT
        # ranges are legitimate exploration and stay quiet. (Identical-argument
        # repeats now trip the debounce at 4 — see
        # test_loop_guard_readfile_debounce.py, #1092.)
        msgs = [{"role": "user", "content": "explore"}]
        for i in range(7):
            cid = f"c{i}"
            msgs.append(
                _asst("read_file", args=json.dumps({"path": "a.py", "offset": i * 100}), call_id=cid)
            )
            msgs.append(_result("ok", call_id=cid))
        assert maybe_nudge(msgs) is None

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


class TestExplorationAlternativeHint:
    """#625: 16-17 consecutive read_file / 14 consecutive search_files calls
    in real sessions, never reaching for repo_map or delegate_task. The
    mono-tool spiral nudge for these two tools specifically should surface
    those alternatives — other tools' nudges must stay unchanged."""

    @staticmethod
    def _varied_args_run(tool, n, *, result="ok"):
        """n consecutive single-tool turns with DIFFERENT arguments each —
        avoids the #467 same-query short-circuit so the generic mono-tool
        repeat_threshold path is what actually fires."""
        msgs = [{"role": "user", "content": "explore the codebase"}]
        for i in range(n):
            cid = f"c{i}"
            msgs.append(
                _asst(tool, args=json.dumps({"path": f"file_{i}.py"}), call_id=cid)
            )
            msgs.append(_result(result, call_id=cid))
        return msgs

    def test_read_file_regular_nudge_suggests_repo_map_first(self):
        # read_file is idempotent (repeat_threshold=8); 8 calls -> regular nudge.
        n = maybe_nudge(_run("read_file", 8))
        assert n is not None
        assert "repo_map" in n and "delegate_task" in n
        # #625 follow-up: the hint should explicitly advise repo_map FIRST.
        assert "call `repo_map` FIRST" in n

    def test_search_files_regular_nudge_suggests_repo_map_first(self):
        # search_files is short-circuit-prone on SAME args (#467); use varied
        # args so the generic repeat_threshold path fires instead, matching
        # #625's actual pattern (different queries, not a repeated one).
        n = maybe_nudge(self._varied_args_run("search_files", 8))
        assert n is not None
        assert "repo_map" in n and "delegate_task" in n
        assert "call `repo_map` FIRST" in n

    def test_read_file_regular_nudge_suggests_alternatives(self):
        # read_file is idempotent (repeat_threshold=8); 8 calls -> regular nudge.
        n = maybe_nudge(_run("read_file", 8))
        assert n is not None
        assert "repo_map" in n and "delegate_task" in n

    def test_search_files_regular_nudge_suggests_alternatives(self):
        # search_files is short-circuit-prone on SAME args (#467); use varied
        # args so the generic repeat_threshold path fires instead, matching
        # #625's actual pattern (different queries, not a repeated one).
        n = maybe_nudge(self._varied_args_run("search_files", 8))
        assert n is not None
        assert "repo_map" in n and "delegate_task" in n

    def test_read_file_escalated_nudge_suggests_alternatives(self):
        # idempotent escalate_threshold=15.
        n = maybe_nudge(_run("read_file", 15))
        assert n is not None
        assert "ESCALATED INTERRUPT" in n
        assert "repo_map" in n and "delegate_task" in n

    def test_search_files_escalated_nudge_suggests_alternatives(self):
        n = maybe_nudge(self._varied_args_run("search_files", 15))
        assert n is not None
        assert "ESCALATED INTERRUPT" in n
        assert "repo_map" in n and "delegate_task" in n

    def test_unrelated_tool_nudge_has_no_exploration_hint(self):
        n = maybe_nudge(_run("terminal", 4))
        assert n is not None
        assert "repo_map" not in n and "delegate_task" not in n

    def test_write_file_nudge_has_no_exploration_hint(self):
        n = maybe_nudge(_run("write_file", 4))
        assert n is not None
        assert "repo_map" not in n and "delegate_task" not in n

    def test_same_query_short_circuit_has_no_exploration_hint(self):
        # #467 same-query short-circuit is a DIFFERENT problem (repeating the
        # identical query) with its own, already-relevant advice — the
        # exploration hint must not bleed into this path.
        msgs = _run(
            "search_files", 4
        )  # identical args -> short-circuit, not repeat_threshold
        n = maybe_nudge(msgs)
        assert n is not None
        assert "SAME arguments" in n  # confirms the short-circuit path fired
        assert "repo_map" not in n and "delegate_task" not in n


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
            msgs.append(
                _asst("web_search", args={"query": "best cat food"}, call_id=cid)
            )
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
        # fire (varied args), but the #1012 diverse-query cap fires at 6
        # (before the generic repeat threshold of 8). The nudge should mention
        # "STOP searching" — the #1012 synthesis nudge — not "SAME arguments".
        msgs = self._web_run([f'{{"query": "topic {i}"}}' for i in range(8)])
        n = maybe_nudge(msgs)
        assert n is not None
        assert "SAME arguments" not in n  # not the #467 short-circuit
        assert "web_search" in n  # the nudge still fires
        assert "STOP searching" in n  # #1012 diverse-query synthesis nudge


class TestWebSearchDiverseQueryCap:
    """#1012 — web_search with *different* queries is a distinct spiral from
    the same-argument loop (#467). The model reformulates the query each time,
    each call "succeeds", but it never stops to synthesize. The diverse-query
    cap fires at 6 (below the generic repeat threshold of 8) with a nudge that
    explicitly directs synthesis."""

    def _web_run(self, queries, *, result="3 relevant hits"):
        msgs = [{"role": "user", "content": "research the topic"}]
        for i, q in enumerate(queries):
            cid = f"c{i}"
            msgs.append(_asst("web_search", args=q, call_id=cid))
            msgs.append(_result(result, call_id=cid))
        return msgs

    def test_diverse_queries_below_cap_quiet(self):
        # 5 different queries < cap (6) → no nudge.
        msgs = self._web_run([f'{{"query": "topic {i}"}}' for i in range(5)])
        assert maybe_nudge(msgs) is None

    def test_diverse_queries_at_cap_nudges_synthesis(self):
        # 6 different queries ≥ cap (6) → synthesis nudge.
        msgs = self._web_run([f'{{"query": "topic {i}"}}' for i in range(6)])
        n = maybe_nudge(msgs)
        assert n is not None
        assert "STOP searching" in n
        assert "synthesize" in n
        assert "loop-guard" in n

    def test_diverse_queries_above_cap_still_nudges(self):
        # 7 different queries > cap (6) and < repeat_threshold (8) → nudge.
        msgs = self._web_run([f'{{"query": "topic {i}"}}' for i in range(7)])
        n = maybe_nudge(msgs)
        assert n is not None
        assert "STOP searching" in n

    def test_same_queries_not_caught_by_diverse_cap(self):
        # 6 identical queries: the same-arg short-circuit fires first (at 4),
        # not the diverse-query cap. Verify the nudge is the same-arg one.
        msgs = self._web_run(['{"query": "same query"}'] * 6)
        n = maybe_nudge(msgs)
        assert n is not None
        assert "SAME arguments" in n
        assert "STOP searching" not in n


class TestShouldCronHardStop:
    """#624: unattended cron turns get real enforcement, not just advisory
    text — interactive surfaces (a human is present) are never affected."""

    def test_below_threshold_never_stops(self):
        assert (
            should_cron_hard_stop("cron", CRON_LOOP_GUARD_HARD_STOP_THRESHOLD - 1)
            is False
        )

    def test_at_threshold_stops(self):
        assert (
            should_cron_hard_stop("cron", CRON_LOOP_GUARD_HARD_STOP_THRESHOLD) is True
        )

    def test_above_threshold_stops(self):
        assert (
            should_cron_hard_stop("cron", CRON_LOOP_GUARD_HARD_STOP_THRESHOLD + 5)
            is True
        )

    def test_zero_warnings_never_stops(self):
        assert should_cron_hard_stop("cron", 0) is False

    def test_non_cron_platforms_never_stop_regardless_of_count(self):
        huge_count = CRON_LOOP_GUARD_HARD_STOP_THRESHOLD + 100
        for platform in ("cli", "telegram", "discord", "gateway", None, ""):
            assert should_cron_hard_stop(platform, huge_count) is False, platform


class TestShouldInteractiveHardStop:
    """#1109–#1112: interactive failing-spiral enforcement. After enough
    advisory warnings for a GENUINE failing spiral (run_warrants_cron_hard_stop
    is True), interactive surfaces also stop — not just cron. Legitimate
    repetitive-but-successful work is never affected."""

    def test_below_threshold_never_stops(self):
        assert (
            should_interactive_hard_stop(
                "cli",
                INTERACTIVE_LOOP_GUARD_HARD_STOP_THRESHOLD - 1,
                genuine_spiral=True,
            )
            is False
        )

    def test_at_threshold_stops_for_genuine_spiral(self):
        assert (
            should_interactive_hard_stop(
                "cli",
                INTERACTIVE_LOOP_GUARD_HARD_STOP_THRESHOLD,
                genuine_spiral=True,
            )
            is True
        )

    def test_above_threshold_stops(self):
        assert (
            should_interactive_hard_stop(
                "telegram",
                INTERACTIVE_LOOP_GUARD_HARD_STOP_THRESHOLD + 5,
                genuine_spiral=True,
            )
            is True
        )

    def test_never_stops_for_non_genuine_spiral(self):
        """Repetitive-but-successful work: keep nudging, never hard-stop."""
        huge = INTERACTIVE_LOOP_GUARD_HARD_STOP_THRESHOLD + 100
        for platform in ("cli", "gateway", "telegram", "discord", None, ""):
            assert should_interactive_hard_stop(
                platform, huge, genuine_spiral=False
            ) is False, platform

    def test_cron_platform_never_triggers_interactive_path(self):
        """Cron uses its own lower-threshold path; should_interactive_hard_stop
        must defer to that and always return False for cron."""
        assert (
            should_interactive_hard_stop(
                "cron",
                INTERACTIVE_LOOP_GUARD_HARD_STOP_THRESHOLD + 10,
                genuine_spiral=True,
            )
            is False
        )

    def test_all_interactive_platforms_covered(self):
        """Every non-cron platform should be eligible once the threshold is
        met for a genuine spiral."""
        for platform in ("cli", "gateway", "telegram", "discord", "web", None, ""):
            assert should_interactive_hard_stop(
                platform,
                INTERACTIVE_LOOP_GUARD_HARD_STOP_THRESHOLD,
                genuine_spiral=True,
            ) is True, platform

    def test_zero_warnings_never_stops(self):
        assert should_interactive_hard_stop("cli", 0, genuine_spiral=True) is False


class TestFailureClassAndDiversionHints:
    """#694 family: the generic failure nudge names the dominant error class
    with its tool_diagnostics recovery hint (#365), and the observed spiral
    tools get concrete diversion advice (#680 #697 #698 #699 #700)."""

    def _fail_run(self, tool, n, content):
        msgs = [{"role": "user", "content": "go"}]
        for i in range(n):
            cid = f"c{i}"
            msgs.append(_asst(tool, call_id=cid))
            msgs.append(_result(content, call_id=cid))
        return msgs

    def test_generic_fail_nudge_names_dominant_class(self):
        # "does not exist" classifies as not_found (retryable) → generic branch
        n = maybe_nudge(self._fail_run("terminal", 2, "error: config.yaml does not exist"))
        assert n is not None
        assert "not_found" in n
        assert "Re-check the path" in n

    def test_patch_failures_get_reread_diversion(self):
        n = maybe_nudge(self._fail_run("patch", 2, "error: old content not found in file"))
        assert n is not None
        assert "read_file" in n

    def test_memory_failures_get_recall_diversion(self):
        n = maybe_nudge(self._fail_run("memory", 2, "error: memory write failed"))
        assert n is not None
        assert "session_search" in n

    def test_tool_call_failures_get_routing_diversion(self):
        n = maybe_nudge(self._fail_run("tool_call", 2, "error: unknown tool 'frobnicate'"))
        assert n is not None
        assert "tool name" in n

    def test_browser_navigate_repeat_gets_extraction_diversion(self):
        n = maybe_nudge(_run("browser_navigate", 4))
        assert n is not None
        assert "browser_snapshot" in n

    def test_web_search_reformulation_run_gets_synthesis_diversion(self):
        # #700's actual shape: many DIFFERENT queries (reformulations), each
        # "succeeding", no synthesis. Hits the generic repeat path (idempotent
        # threshold = 8).
        msgs = [{"role": "user", "content": "go"}]
        for i in range(8):
            cid = f"c{i}"
            msgs.append(
                _asst("web_search", args=json.dumps({"query": f"attempt {i}"}), call_id=cid)
            )
            msgs.append(
                _result("<untrusted_tool_result>10 hits</untrusted_tool_result>", call_id=cid)
            )
        n = maybe_nudge(msgs)
        assert n is not None
        assert "synthesize" in n.lower() or "web_extract" in n

    def test_web_search_same_args_branch_has_no_contradicting_hint(self):
        # The identical-args short-circuit (#467) carries NO appended hints —
        # its own advice ('rephrase the query') would be directly contradicted
        # by the 'stop reformulating' diversion (consult review).
        msgs = [{"role": "user", "content": "go"}]
        for i in range(4):
            cid = f"c{i}"
            msgs.append(
                _asst("web_search", args=json.dumps({"query": "same"}), call_id=cid)
            )
            msgs.append(
                _result("<untrusted_tool_result>10 hits</untrusted_tool_result>", call_id=cid)
            )
        n = maybe_nudge(msgs)
        assert n is not None and "SAME arguments" in n
        assert "Stop reformulating" not in n

    def test_read_file_exploration_hint_survives(self):
        # #625 hint must not be displaced by the new diversion table.
        n = maybe_nudge(_run("read_file", 8))
        assert n is not None
        assert "repo_map" in n

    def test_vision_analyze_failures_get_media_diversion(self):
        # #739: a failed visual call is redirected to a check / text fallback,
        # not a blind retry.
        n = maybe_nudge(
            self._fail_run("vision_analyze", 2, "vision_analyze failed: invalid image")
        )
        assert n is not None
        assert "read_file" in n

    def test_image_generate_failures_get_media_diversion(self):
        # #739: repeated generation failures route to a text/placeholder fallback.
        n = maybe_nudge(
            self._fail_run("image_generate", 2, "image generation failed: provider down")
        )
        assert n is not None
        assert "placeholder" in n or "provider" in n


def _distinct_run(tool, n, *, result="ok"):
    """n consecutive single-tool turns with DISTINCT arguments each (real
    progress: a burst of different successful commands)."""
    msgs = [{"role": "user", "content": "finalize the change"}]
    for i in range(n):
        cid = f"c{i}"
        msgs.append(_asst(tool, args=json.dumps({"command": f"step-{i}"}), call_id=cid))
        msgs.append(_result(result, call_id=cid))
    return msgs


class TestRunWarrantsCronHardStop:
    """The second #624 gate: a cron hard stop is warranted ONLY for genuine
    non-progress, never for legitimate mono-tool work that merely looks
    repetitive (the evolution implementation/integration false positives)."""

    def test_distinct_successful_terminal_burst_does_not_warrant(self):
        # Implementation job: lint -> git add -> commit -> push -> gh pr create,
        # 8 DISTINCT successful `terminal` calls. Real progress, not a spiral.
        assert run_warrants_cron_hard_stop(_distinct_run("terminal", 8)) is False

    def test_identical_successful_terminal_repeat_warrants(self):
        # Canonical #624 degenerate loop: the SAME successful `echo hi` x N.
        # Identical args + no progress -> hard stop stays warranted.
        assert run_warrants_cron_hard_stop(_run("terminal", 6)) is True

    def test_oscillating_successful_terminal_cycle_is_spared(self):
        # Cyclic INSPECTION with varied args (git status -> git diff A ->
        # git diff B -> ...) is legitimate progress with intervening state
        # changes, NOT a spiral. A cross-provider review (Kimi) flagged that a
        # diversity ratio would false-positive here and kill real merge-
        # resolution work, so the gate deliberately spares varied-arg cycles
        # (they are backstopped by the iteration budget, not hard-stopped).
        msgs = [{"role": "user", "content": "figure out the state"}]
        for i in range(6):
            cid = f"c{i}"
            cmd = "git status" if i % 2 == 0 else "git diff"
            msgs.append(_asst("terminal", args=json.dumps({"command": cmd}), call_id=cid))
            msgs.append(_result("ok", call_id=cid))
        assert run_warrants_cron_hard_stop(msgs) is False

    def test_process_polling_identical_success_does_not_warrant(self):
        # Integration job: polling a background `process` handle while waiting
        # for CI. Identical successful polls are legitimate waiting, not a loop.
        assert run_warrants_cron_hard_stop(_run("process", 8)) is False

    def test_process_repeated_failure_still_warrants(self):
        # A polling tool that keeps ERRORING is a real problem — the failure
        # branch applies even to exempt polling tools.
        msgs = _run("process", 3, result="error: process crashed")
        assert run_warrants_cron_hard_stop(msgs) is True

    def test_failing_terminal_spiral_warrants(self):
        # The real-world #624 case: terminal retried unchanged, failing.
        msgs = _run("terminal", 3, result="error: build step blew up")
        assert run_warrants_cron_hard_stop(msgs) is True

    def test_no_tools_does_not_warrant(self):
        assert run_warrants_cron_hard_stop([{"role": "user", "content": "hi"}]) is False

    def test_distinct_process_calls_do_not_warrant(self):
        # Even distinct successful process calls (varied handles) are fine.
        assert run_warrants_cron_hard_stop(_distinct_run("process", 8)) is False


# ── Refusal nudge tests (#975) ─────────────────────────────────────────


def _asst_text(content: str) -> dict:
    """Build an assistant text-only message (no tool calls)."""
    return {"role": "assistant", "content": content}


class TestMaybeRefusalNudge:
    """Tests for maybe_refusal_nudge — refusal detection in assistant text."""

    def test_no_refusal_returns_none(self):
        msgs = [
            {"role": "user", "content": "do the task"},
            _asst_text("I'll help you with that. Let me start by running the tests."),
        ]
        assert maybe_refusal_nudge(msgs) is None

    def test_over_refusal_detected(self):
        msgs = [
            {"role": "user", "content": "run the build"},
            _asst_text("I can't do that right now."),
        ]
        nudge = maybe_refusal_nudge(msgs)
        assert nudge is not None
        assert "over_refusal" in nudge
        assert "[loop-guard]" in nudge

    def test_capability_gap_detected(self):
        msgs = [
            {"role": "user", "content": "deploy to kubernetes"},
            _asst_text("I don't have a tool for that."),
        ]
        nudge = maybe_refusal_nudge(msgs)
        assert nudge is not None
        assert "true_capability_gap" in nudge

    def test_permission_boundary_detected(self):
        msgs = [
            {"role": "user", "content": "read /etc/shadow"},
            _asst_text("I don't have permission to access that file."),
        ]
        nudge = maybe_refusal_nudge(msgs)
        assert nudge is not None
        assert "permission_boundary" in nudge

    def test_rhetorical_false_positive_ignored(self):
        """'I can't stress this enough' is NOT a refusal."""
        msgs = [
            {"role": "user", "content": "review the code"},
            _asst_text("I can't stress this enough — the code looks great!"),
        ]
        assert maybe_refusal_nudge(msgs) is None

    def test_already_nudged_returns_none(self):
        msgs = [
            {"role": "user", "content": "do the thing"},
            _asst_text("I can't help with that."),
        ]
        assert maybe_refusal_nudge(msgs, already_nudged=True) is None

    def test_no_assistant_message_returns_none(self):
        msgs = [
            {"role": "user", "content": "hello"},
        ]
        assert maybe_refusal_nudge(msgs) is None

    def test_empty_assistant_content_returns_none(self):
        msgs = [
            {"role": "user", "content": "hello"},
            _asst_text(""),
        ]
        assert maybe_refusal_nudge(msgs) is None

    def test_synthetic_sentinel_skipped(self):
        """Empty terminal sentinel messages are skipped when looking for text."""
        msgs = [
            {"role": "user", "content": "do the thing"},
            {"role": "assistant", "content": "I can't do that.", "role": "assistant"},
            {"role": "assistant", "content": "(empty)", "_empty_terminal_sentinel": True},
        ]
        # Should find the real refusal text, not the sentinel
        nudge = maybe_refusal_nudge(msgs)
        assert nudge is not None
        assert "over_refusal" in nudge

    def test_nudge_contains_recovery_directive(self):
        msgs = [
            {"role": "user", "content": "build the project"},
            _asst_text("I'm unable to build the project."),
        ]
        nudge = maybe_refusal_nudge(msgs)
        assert nudge is not None
        assert "Do not simply repeat the refusal" in nudge
        assert "concrete action" in nudge

    def test_multiple_refusals_still_nudges_once(self):
        msgs = [
            {"role": "user", "content": "do x and y"},
            _asst_text("I can't do x. I also don't have access to y."),
        ]
        nudge = maybe_refusal_nudge(msgs)
        assert nudge is not None
        # Should classify the first refusal
        assert "[loop-guard]" in nudge
