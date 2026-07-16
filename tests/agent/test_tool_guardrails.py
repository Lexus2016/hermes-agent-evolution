"""Pure tool-call guardrail primitive tests."""

import json

from agent.tool_guardrails import (
    ToolCallGuardrailConfig,
    ToolCallGuardrailController,
    ToolCallSignature,
    append_toolguard_guidance,
    canonical_tool_args,
    classify_tool_failure,
    toolguard_synthetic_result,
)


def test_tool_call_signature_hashes_canonical_nested_unicode_args_without_exposing_raw_args():
    args_a = {
        "z": [{"β": "☤", "a": 1}],
        "a": {"y": 2, "x": "secret-token-value"},
    }
    args_b = {
        "a": {"x": "secret-token-value", "y": 2},
        "z": [{"a": 1, "β": "☤"}],
    }

    assert canonical_tool_args(args_a) == canonical_tool_args(args_b)
    sig_a = ToolCallSignature.from_call("web_search", args_a)
    sig_b = ToolCallSignature.from_call("web_search", args_b)

    assert sig_a == sig_b
    assert len(sig_a.args_hash) == 64
    metadata = sig_a.to_metadata()
    assert metadata == {"tool_name": "web_search", "args_hash": sig_a.args_hash}
    assert "secret-token-value" not in json.dumps(metadata)
    assert "☤" not in json.dumps(metadata)


def test_default_config_is_soft_warning_only_with_hard_stop_disabled():
    cfg = ToolCallGuardrailConfig()

    assert cfg.warnings_enabled is True
    assert cfg.hard_stop_enabled is False
    assert cfg.exact_failure_warn_after == 2
    assert cfg.same_tool_failure_warn_after == 3
    assert cfg.no_progress_warn_after == 2
    assert cfg.exact_failure_block_after == 5
    assert cfg.same_tool_failure_halt_after == 8
    assert cfg.no_progress_block_after == 5
    assert cfg.browser_failure_cap == 3


def test_config_parses_nested_warn_and_hard_stop_thresholds():
    cfg = ToolCallGuardrailConfig.from_mapping(
        {
            "warnings_enabled": False,
            "hard_stop_enabled": True,
            "warn_after": {
                "exact_failure": 3,
                "same_tool_failure": 4,
                "idempotent_no_progress": 5,
            },
            "hard_stop_after": {
                "exact_failure": 6,
                "same_tool_failure": 7,
                "idempotent_no_progress": 8,
            },
        }
    )

    assert cfg.warnings_enabled is False
    assert cfg.hard_stop_enabled is True
    assert cfg.exact_failure_warn_after == 3
    assert cfg.same_tool_failure_warn_after == 4
    assert cfg.no_progress_warn_after == 5
    assert cfg.exact_failure_block_after == 6
    assert cfg.same_tool_failure_halt_after == 7
    assert cfg.no_progress_block_after == 8


def test_default_repeated_identical_failed_call_warns_without_blocking():
    controller = ToolCallGuardrailController()
    args = {"query": "same"}

    decisions = []
    for _ in range(5):
        assert controller.before_call("web_search", args).action == "allow"
        decisions.append(
            controller.after_call("web_search", args, '{"error":"boom"}', failed=True)
        )

    assert decisions[0].action == "allow"
    assert [d.action for d in decisions[1:]] == ["warn", "warn", "warn", "warn"]
    assert {d.code for d in decisions[1:]} == {"repeated_exact_failure_warning"}
    assert controller.before_call("web_search", args).action == "allow"
    assert controller.halt_decision is None


def test_hard_stop_enabled_blocks_repeated_exact_failure_before_next_execution():
    controller = ToolCallGuardrailController(
        ToolCallGuardrailConfig(
            hard_stop_enabled=True,
            exact_failure_warn_after=2,
            exact_failure_block_after=2,
            same_tool_failure_halt_after=99,
        )
    )
    args = {"query": "same"}

    assert controller.before_call("web_search", args).action == "allow"
    first = controller.after_call("web_search", args, '{"error":"boom"}', failed=True)
    assert first.action == "allow"

    assert controller.before_call("web_search", args).action == "allow"
    second = controller.after_call("web_search", args, '{"error":"boom"}', failed=True)
    assert second.action == "warn"
    assert second.code == "repeated_exact_failure_warning"

    blocked = controller.before_call("web_search", args)
    assert blocked.action == "block"
    assert blocked.code == "repeated_exact_failure_block"
    assert blocked.count == 2


def test_success_resets_exact_signature_failure_streak():
    controller = ToolCallGuardrailController(
        ToolCallGuardrailConfig(hard_stop_enabled=True, exact_failure_block_after=2, same_tool_failure_halt_after=99)
    )
    args = {"query": "same"}

    controller.after_call("web_search", args, '{"error":"boom"}', failed=True)
    controller.after_call("web_search", args, '{"ok":true}', failed=False)

    assert controller.before_call("web_search", args).action == "allow"
    controller.after_call("web_search", args, '{"error":"boom"}', failed=True)
    assert controller.before_call("web_search", args).action == "allow"


def test_file_mutation_lint_error_result_is_not_a_tool_failure():
    write_result = json.dumps({
        "bytes_written": 12,
        "lint": {"status": "error", "output": "SyntaxError: invalid syntax"},
    })
    patch_result = json.dumps({
        "success": True,
        "diff": "--- a/tmp.py\n+++ b/tmp.py\n",
        "lsp_diagnostics": "<diagnostics>ERROR [1:1] type mismatch</diagnostics>",
    })

    assert classify_tool_failure("write_file", write_result) == (False, "")
    assert classify_tool_failure("patch", patch_result) == (False, "")


def test_same_tool_varying_args_warns_by_default_without_halting():
    controller = ToolCallGuardrailController(
        ToolCallGuardrailConfig(same_tool_failure_warn_after=2, same_tool_failure_halt_after=3)
    )

    first = controller.after_call("terminal", {"command": "cmd-1"}, '{"exit_code":1}', failed=True)
    second = controller.after_call("terminal", {"command": "cmd-2"}, '{"exit_code":1}', failed=True)
    third = controller.after_call("terminal", {"command": "cmd-3"}, '{"exit_code":1}', failed=True)
    fourth = controller.after_call("terminal", {"command": "cmd-4"}, '{"exit_code":1}', failed=True)

    assert first.action == "allow"
    assert [second.action, third.action, fourth.action] == ["warn", "warn", "warn"]
    assert {second.code, third.code, fourth.code} == {"same_tool_failure_warning"}
    assert "Do not switch to text-only replies" in second.message
    assert "keep using tools" in second.message
    assert "diagnose before retrying" in second.message
    assert "different tool" in second.message
    assert controller.halt_decision is None


def test_hard_stop_enabled_halts_same_tool_varying_args_failure_streak():
    controller = ToolCallGuardrailController(
        ToolCallGuardrailConfig(
            hard_stop_enabled=True,
            exact_failure_block_after=99,
            same_tool_failure_warn_after=2,
            same_tool_failure_halt_after=3,
        )
    )

    first = controller.after_call("terminal", {"command": "cmd-1"}, '{"exit_code":1}', failed=True)
    assert first.action == "allow"
    second = controller.after_call("terminal", {"command": "cmd-2"}, '{"exit_code":1}', failed=True)
    assert second.action == "warn"
    assert second.code == "same_tool_failure_warning"
    third = controller.after_call("terminal", {"command": "cmd-3"}, '{"exit_code":1}', failed=True)
    assert third.action == "halt"
    assert third.code == "same_tool_failure_halt"
    assert third.count == 3


def test_idempotent_no_progress_repeated_result_warns_without_blocking_by_default():
    controller = ToolCallGuardrailController(
        ToolCallGuardrailConfig(no_progress_warn_after=2, no_progress_block_after=2)
    )
    args = {"path": "/tmp/same.txt"}
    result = "same file contents"

    for _ in range(4):
        assert controller.before_call("read_file", args).action == "allow"
        decision = controller.after_call("read_file", args, result, failed=False)

    assert decision.action == "warn"
    assert decision.code == "idempotent_no_progress_warning"
    assert controller.before_call("read_file", args).action == "allow"
    assert controller.halt_decision is None


def test_hard_stop_enabled_blocks_idempotent_no_progress_future_repeat():
    controller = ToolCallGuardrailController(
        ToolCallGuardrailConfig(
            hard_stop_enabled=True,
            no_progress_warn_after=2,
            no_progress_block_after=2,
        )
    )
    args = {"path": "/tmp/same.txt"}
    result = "same file contents"

    assert controller.before_call("read_file", args).action == "allow"
    assert controller.after_call("read_file", args, result, failed=False).action == "allow"
    assert controller.before_call("read_file", args).action == "allow"
    warn = controller.after_call("read_file", args, result, failed=False)
    assert warn.action == "warn"
    assert warn.code == "idempotent_no_progress_warning"

    blocked = controller.before_call("read_file", args)
    assert blocked.action == "block"
    assert blocked.code == "idempotent_no_progress_block"


def test_mutating_or_unknown_tools_are_not_blocked_for_repeated_identical_success_output_by_default():
    controller = ToolCallGuardrailController(
        ToolCallGuardrailConfig(no_progress_warn_after=2, no_progress_block_after=2)
    )

    for _ in range(3):
        assert controller.before_call("write_file", {"path": "/tmp/x", "content": "x"}).action == "allow"
        assert controller.after_call("write_file", {"path": "/tmp/x", "content": "x"}, "ok", failed=False).action == "allow"
        assert controller.before_call("custom_tool", {"x": 1}).action == "allow"
        assert controller.after_call("custom_tool", {"x": 1}, "ok", failed=False).action == "allow"


def test_reset_for_turn_clears_bounded_guardrail_state():
    controller = ToolCallGuardrailController(
        ToolCallGuardrailConfig(hard_stop_enabled=True, exact_failure_block_after=2, no_progress_block_after=2)
    )
    controller.after_call("web_search", {"query": "same"}, '{"error":"boom"}', failed=True)
    controller.after_call("web_search", {"query": "same"}, '{"error":"boom"}', failed=True)
    controller.after_call("read_file", {"path": "/tmp/x"}, "same", failed=False)
    controller.after_call("read_file", {"path": "/tmp/x"}, "same", failed=False)

    assert controller.before_call("web_search", {"query": "same"}).action == "block"
    assert controller.before_call("read_file", {"path": "/tmp/x"}).action == "block"

    controller.reset_for_turn()

    assert controller.before_call("web_search", {"query": "same"}).action == "allow"
    assert controller.before_call("read_file", {"path": "/tmp/x"}).action == "allow"


# ── #744/#785: fallback_directive field on ToolGuardrailDecision ──────────────


def test_fallback_directive_populated_on_same_tool_failure_warning():
    """A repeated same-tool failure warning carries a non-empty fallback_directive."""
    controller = ToolCallGuardrailController()
    args = {"path": "/nonexistent"}
    # read_file is idempotent (fail_threshold for same_tool = 3 by default)
    for _ in range(3):
        controller.before_call("read_file", args)
        decision = controller.after_call("read_file", args, '{"error":"not found"}', failed=True)
    assert decision.action == "warn"
    assert decision.fallback_directive != ""
    assert "search_files" in decision.fallback_directive


def test_fallback_directive_populated_on_exact_failure_warning():
    """A repeated exact-failure warning carries a non-empty fallback_directive."""
    controller = ToolCallGuardrailController()
    args = {"query": "same"}
    # exact_failure_warn_after = 2 by default
    for _ in range(2):
        controller.before_call("web_search", args)
        decision = controller.after_call("web_search", args, '{"error":"boom"}', failed=True)
    assert decision.action == "warn"
    assert decision.fallback_directive != ""
    assert "web_extract" in decision.fallback_directive


def test_fallback_directive_empty_on_allow():
    """A non-failure (allow) decision has an empty fallback_directive."""
    controller = ToolCallGuardrailController()
    controller.before_call("read_file", {"path": "/tmp/x"})
    decision = controller.after_call("read_file", {"path": "/tmp/x"}, "content", failed=False)
    assert decision.action == "allow"
    assert decision.fallback_directive == ""


def test_fallback_directive_empty_for_unknown_tool():
    """An unknown tool without a known fallback gets an empty fallback_directive."""
    controller = ToolCallGuardrailController(
        ToolCallGuardrailConfig(same_tool_failure_warn_after=2)
    )
    args = {"key": "val"}
    for _ in range(2):
        controller.before_call("mcp_custom_tool", args)
        decision = controller.after_call("mcp_custom_tool", args, '{"error":"bad"}', failed=True)
    assert decision.action == "warn"
    assert decision.fallback_directive == ""


def test_fallback_directive_in_metadata():
    """to_metadata() includes fallback_directive when non-empty, omits when empty."""
    controller = ToolCallGuardrailController()
    args = {"path": "/nonexistent"}
    for _ in range(3):
        controller.before_call("read_file", args)
        decision = controller.after_call("read_file", args, '{"error":"not found"}', failed=True)
    assert decision.fallback_directive != ""
    meta = decision.to_metadata()
    assert "fallback_directive" in meta
    assert meta["fallback_directive"] == decision.fallback_directive

    # Allow decisions omit the key entirely
    controller.before_call("read_file", {"path": "/tmp/other"})
    allow_decision = controller.after_call("read_file", {"path": "/tmp/other"}, "ok", failed=False)
    assert "fallback_directive" not in allow_decision.to_metadata()


# ── #739: media-tool fallback directives ──────────────────────────────────────


def test_fallback_directive_populated_for_vision_analyze():
    """Repeated vision_analyze failures carry a media-aware fallback_directive."""
    controller = ToolCallGuardrailController()
    args = {"path": "/bad.png"}
    for _ in range(3):
        controller.before_call("vision_analyze", args)
        decision = controller.after_call(
            "vision_analyze", args, '{"success": false, "error": "invalid image"}', failed=True
        )
    assert decision.action == "warn"
    assert decision.fallback_directive != ""
    assert "read_file" in decision.fallback_directive


def test_fallback_directive_populated_for_image_generate():
    """Repeated image_generate failures route to a text/placeholder fallback."""
    controller = ToolCallGuardrailController()
    args = {"prompt": "a cat"}
    # exact_failure_warn_after = 2 by default
    for _ in range(2):
        controller.before_call("image_generate", args)
        decision = controller.after_call(
            "image_generate", args, '{"success": false, "error": "provider error"}', failed=True
        )
    assert decision.action == "warn"
    assert decision.fallback_directive != ""
    assert "placeholder" in decision.fallback_directive


def test_fallback_directive_covers_video_media_tools():
    """video_analyze / video_generate also carry non-empty fallback directives."""
    from agent.tool_guardrails import _fallback_directive_for

    assert "read_file" in _fallback_directive_for("video_analyze")
    assert "placeholder" in _fallback_directive_for("video_generate")


# ── #787: fallback_directive consumption in guardrail output ──────────────────


def _make_warn_decision_with_directive(
    tool_name: str = "read_file", directive: str = "use search_files instead"
):
    """Build a warn decision with a non-empty fallback_directive for output tests."""
    from agent.tool_guardrails import ToolGuardrailDecision, ToolCallSignature

    return ToolGuardrailDecision(
        action="warn",
        code="repeated_exact_failure_warning",
        message="read_file has failed 2 times with identical arguments.",
        tool_name=tool_name,
        count=2,
        signature=ToolCallSignature.from_call(tool_name, {"path": "/bad"}),
        fallback_directive=directive,
    )


def test_synthetic_result_includes_fallback_directive_as_top_level_field():
    """toolguard_synthetic_result surfaces fallback_directive at the top level (#787)."""
    decision = _make_warn_decision_with_directive(directive="use search_files instead")
    payload = json.loads(toolguard_synthetic_result(decision))

    assert "fallback_directive" in payload
    assert payload["fallback_directive"] == "use search_files instead"
    # The directive is also in the nested guardrail metadata (from #785)
    assert payload["guardrail"]["fallback_directive"] == "use search_files instead"


def test_synthetic_result_omits_fallback_directive_when_empty():
    """When fallback_directive is empty, the top-level key is absent (backward compat)."""
    from agent.tool_guardrails import ToolGuardrailDecision

    decision = ToolGuardrailDecision(
        action="block",
        code="repeated_exact_failure_block",
        message="blocked",
        tool_name="web_search",
        count=5,
        fallback_directive="",
    )
    payload = json.loads(toolguard_synthetic_result(decision))

    assert "fallback_directive" not in payload
    assert "fallback_directive" not in payload.get("guardrail", {})


def test_append_guidance_includes_fallback_directive_in_suffix():
    """append_toolguard_guidance appends the fallback directive as a labelled line (#787)."""
    decision = _make_warn_decision_with_directive(directive="use search_files instead")
    result = append_toolguard_guidance("tool output here", decision)

    assert "[Fallback directive: use search_files instead]" in result
    assert "[Tool loop warning:" in result
    assert result.startswith("tool output here")


def test_append_guidance_omits_fallback_directive_line_when_empty():
    """When fallback_directive is empty, no directive line is appended (backward compat)."""
    from agent.tool_guardrails import ToolGuardrailDecision

    decision = ToolGuardrailDecision(
        action="warn",
        code="repeated_exact_failure_warning",
        message="failed 2 times",
        tool_name="web_search",
        count=2,
        fallback_directive="",
    )
    result = append_toolguard_guidance("output", decision)

    assert "[Fallback directive:" not in result
    assert "[Tool loop warning:" in result


def test_append_guidance_no_directive_for_allow_decision():
    """Allow decisions are unchanged by fallback_directive wiring (#787 regression)."""
    from agent.tool_guardrails import ToolGuardrailDecision

    decision = ToolGuardrailDecision(
        action="allow",
        tool_name="read_file",
        fallback_directive="",
    )
    result = append_toolguard_guidance("output", decision)
    assert result == "output"


# ── #745: browser tool retry-spiral cap (always-on, hard_stop-independent) ─────


def test_browser_failure_cap_halts_spiral_with_hard_stop_off():
    """A browser tool spiral halts at the browser cap even with hard_stop OFF.

    This is the core #745 regression: the 15-consecutive browser_navigate /
    10-consecutive browser_console spirals from the trace must be bounded in the
    default (hard-stop-off) mode, not only when the generic circuit breaker is on.
    """
    controller = ToolCallGuardrailController()  # defaults: hard_stop_enabled=False
    assert controller.config.hard_stop_enabled is False
    assert controller.config.browser_failure_cap == 3

    # Simulate a cross-iteration spiral: same browser tool, varying args (a
    # broken backend fails regardless of URL), each result a failure.
    decisions = []
    for i in range(6):
        # With cross-turn tracking, before_call blocks after the streak
        # reaches the cap.  First 3 calls allow; after 3 failures the
        # 4th before_call blocks (stronger than the old allow-then-halt).
        bc = controller.before_call("browser_navigate", {"url": f"https://x/{i}"})
        if i < 3:
            assert bc.allows_execution
            decisions.append(
                controller.after_call(
                    "browser_navigate",
                    {"url": f"https://x/{i}"},
                    '{"success": false, "error": "Could not connect to Chrome backend"}',
                    failed=True,
                )
            )
        else:
            # After 3 failures, before_call blocks — the spiral is stopped
            # before the tool even executes.
            assert not bc.allows_execution
            assert bc.code == "browser_tool_failure_cap"
            decisions.append(bc)
            break

    # First two failures do not hit the cap (cap=3); the third halts and the
    # spiral is stopped — no unbounded 15-in-a-row.
    assert decisions[0].action == "allow"
    halt = decisions[2]
    assert halt.action == "halt"
    assert halt.should_halt is True
    assert halt.code == "browser_tool_failure_cap"
    assert halt.count == 3
    assert halt.fallback_directive != ""
    assert controller.halt_decision is not None
    assert controller.halt_decision.code == "browser_tool_failure_cap"


def test_browser_failure_cap_applies_to_console_and_click():
    """The cap covers every browser_* tool, not just navigate (browser_console
    spiraled 10× in the trace)."""
    for tool in ("browser_console", "browser_click", "browser_type"):
        controller = ToolCallGuardrailController()
        last = None
        for _ in range(3):
            last = controller.after_call(
                tool, {"x": 1}, '{"success": false, "error": "boom"}', failed=True
            )
        assert last.action == "halt", tool
        assert last.code == "browser_tool_failure_cap", tool
        assert last.tool_name == tool


def test_browser_cap_does_not_fire_before_threshold():
    """Below the cap, browser failures only warn — the cap does not over-trigger."""
    controller = ToolCallGuardrailController()
    first = controller.after_call("browser_navigate", {"url": "u"}, '{"error":"boom"}', failed=True)
    second = controller.after_call("browser_navigate", {"url": "u"}, '{"error":"boom"}', failed=True)
    assert first.action == "allow"
    assert second.action == "warn"  # exact_failure_warn_after == 2
    assert controller.halt_decision is None


def test_browser_cap_can_be_disabled():
    """browser_failure_cap=0 disables the browser cap; spirals then follow the
    generic same-tool behaviour (warn-only when hard_stop is off)."""
    controller = ToolCallGuardrailController(ToolCallGuardrailConfig(browser_failure_cap=0))
    decisions = [
        controller.after_call(
            "browser_navigate", {"url": f"u{i}"}, '{"error":"boom"}', failed=True
        )
        for i in range(6)
    ]
    assert all(d.action != "halt" for d in decisions)
    assert controller.halt_decision is None


def test_browser_cap_leaves_native_tool_hard_stop_semantics_unchanged():
    """The always-on browser cap must not leak into non-spiral-prone native
    tools: with hard_stop OFF, a non-spiral same-tool failure spiral still
    only warns (never halts).  Note: terminal and execute_code ARE now
    spiral-prone (see test_spiral_* below), so we use write_file here."""
    controller = ToolCallGuardrailController()  # hard_stop off
    decisions = [
        controller.after_call(
            "write_file", {"path": f"p-{i}"}, '{"error":"boom"}', failed=True
        )
        for i in range(10)
    ]
    assert all(d.action != "halt" for d in decisions)
    assert controller.halt_decision is None


def test_browser_cap_success_resets_streak():
    """A successful browser call clears the failure streak, so the cap only
    fires on a genuine consecutive spiral."""
    controller = ToolCallGuardrailController()
    controller.after_call("browser_navigate", {"url": "u"}, '{"error":"boom"}', failed=True)
    controller.after_call("browser_navigate", {"url": "u"}, '{"error":"boom"}', failed=True)
    # A success resets the same-tool failure count.
    controller.after_call("browser_navigate", {"url": "u"}, '{"success": true}', failed=False)
    # Two more failures should NOT reach the cap (streak restarted at 1, 2).
    d1 = controller.after_call("browser_navigate", {"url": "u"}, '{"error":"boom"}', failed=True)
    d2 = controller.after_call("browser_navigate", {"url": "u"}, '{"error":"boom"}', failed=True)
    assert d1.action != "halt"
    assert d2.action != "halt"
    assert controller.halt_decision is None


def test_browser_cap_reset_for_turn_clears_streak():
    """Per-turn reset clears the halt decision but NOT the cross-turn streak."""
    controller = ToolCallGuardrailController()
    for _ in range(3):
        controller.after_call("browser_navigate", {"url": "u"}, '{"error":"boom"}', failed=True)
    assert controller.halt_decision is not None
    controller.reset_for_turn()
    assert controller.halt_decision is None
    # The cross-turn streak persists — before_call now blocks the browser tool.
    d = controller.before_call("browser_navigate", {"url": "u"})
    assert d.action == "block"
    assert d.code == "browser_tool_failure_cap"
    # A success after reset clears the cross-turn streak.
    controller.reset_for_turn()
    controller.before_call("browser_navigate", {"url": "u"})
    d_ok = controller.after_call("browser_navigate", {"url": "u"}, '{"ok":true}', failed=False)
    assert d_ok.action == "allow"
    controller.reset_for_turn()
    assert controller.before_call("browser_navigate", {"url": "u"}).action == "allow"


def test_browser_failure_cap_parsed_from_mapping():
    cfg = ToolCallGuardrailConfig.from_mapping({"browser_failure_cap": 5})
    assert cfg.browser_failure_cap == 5
    # 0 is honoured (disables); negative falls back to default.
    assert ToolCallGuardrailConfig.from_mapping({"browser_failure_cap": 0}).browser_failure_cap == 0
    assert ToolCallGuardrailConfig.from_mapping({"browser_failure_cap": -3}).browser_failure_cap == 3
    assert ToolCallGuardrailConfig.from_mapping({}).browser_failure_cap == 3


def test_browser_fallback_directive_for_all_browser_tools():
    """Every browser_* tool resolves a non-empty fallback directive (explicit or
    the generic browser default)."""
    from agent.tool_guardrails import _fallback_directive_for

    assert "web_extract" in _fallback_directive_for("browser_navigate")
    assert "snapshot" in _fallback_directive_for("browser_click")
    # An unlisted browser tool still gets the generic browser directive.
    assert _fallback_directive_for("browser_get_images") == (
        "stop re-driving the browser; use web_extract/web_search on the target URL, "
        "or work from the page text already retrieved, instead of retrying"
    )
    # Non-browser unknown tools remain empty (unchanged behaviour).
    assert _fallback_directive_for("mcp_custom_tool") == ""


# ── #974/#969/#970 — spiral-prone tool failure cap ──────────────────────

def test_spiral_cap_halts_terminal_after_threshold():
    """Terminal failures hit the always-on spiral cap (default 5) and halt,
    regardless of hard_stop_enabled.  This is the core fix for #974:
    1237 terminal failures / 410 sessions despite 4 prior fixes — the
    loop_guard's fallback_directive was advisory and the agent ignored it."""
    controller = ToolCallGuardrailController()  # hard_stop OFF (default)
    decisions = []
    for i in range(6):
        controller.before_call("terminal", {"command": f"cmd-{i}"})
        decisions.append(
            controller.after_call(
                "terminal",
                {"command": f"cmd-{i}"},
                '{"exit_code": 1, "error": "boom"}',
                failed=True,
            )
        )
    # First 4 failures do not hit the cap (cap=5); the 5th halts.
    for d in decisions[:4]:
        assert d.action != "halt", f"cap fired too early at {d.count}"
    halt = decisions[4]
    assert halt.action == "halt"
    assert halt.should_halt is True
    assert halt.code == "spiral_prone_tool_failure_cap"
    assert halt.count == 5
    assert halt.fallback_directive != ""
    assert "read_file" in halt.fallback_directive or "diagnostic" in halt.fallback_directive
    assert controller.halt_decision is not None
    assert controller.halt_decision.code == "spiral_prone_tool_failure_cap"


def test_spiral_cap_halts_execute_code_after_threshold():
    """execute_code failures hit the spiral cap and halt (#969: 59 failures /
    14 sessions, max 17 consecutive retries)."""
    controller = ToolCallGuardrailController()
    decisions = []
    for i in range(6):
        decisions.append(
            controller.after_call(
                "execute_code",
                {"code": f"print({i})"},
                '{"error": "NameError: name not defined"}',
                failed=True,
            )
        )
    halt = decisions[4]
    assert halt.action == "halt"
    assert halt.code == "spiral_prone_tool_failure_cap"
    assert halt.tool_name == "execute_code"
    assert halt.fallback_directive != ""


def test_spiral_cap_halts_read_file_after_threshold():
    """read_file failures hit the spiral cap and halt (#970: 26 failures,
    10 sessions with ≥5 consecutive reads)."""
    controller = ToolCallGuardrailController()
    decisions = []
    for i in range(6):
        decisions.append(
            controller.after_call(
                "read_file",
                {"path": f"/nonexistent/{i}"},
                '{"error": "File not found"}',
                failed=True,
            )
        )
    halt = decisions[4]
    assert halt.action == "halt"
    assert halt.code == "spiral_prone_tool_failure_cap"
    assert halt.tool_name == "read_file"
    assert halt.fallback_directive != ""


def test_spiral_cap_does_not_fire_before_threshold():
    """Below the cap, spiral-prone tool failures only warn — the cap does not
    over-trigger.  Uses the same command twice so exact_failure_warn_after (2)
    fires on the second call, matching the browser cap test pattern."""
    controller = ToolCallGuardrailController()
    first = controller.after_call("terminal", {"command": "same"}, '{"exit_code":1}', failed=True)
    second = controller.after_call("terminal", {"command": "same"}, '{"exit_code":1}', failed=True)
    assert first.action == "allow"
    assert second.action == "warn"  # exact_failure_warn_after == 2
    assert controller.halt_decision is None


def test_spiral_cap_can_be_disabled():
    """spiral_failure_cap=0 disables the cap; spirals then follow the generic
    same-tool behaviour (warn-only when hard_stop is off)."""
    controller = ToolCallGuardrailController(ToolCallGuardrailConfig(spiral_failure_cap=0))
    decisions = [
        controller.after_call(
            "terminal", {"command": f"cmd-{i}"}, '{"exit_code":1}', failed=True
        )
        for i in range(10)
    ]
    assert all(d.action != "halt" for d in decisions)
    assert controller.halt_decision is None


def test_spiral_cap_success_resets_streak():
    """A successful terminal call clears the failure streak, so the cap only
    fires on a genuine consecutive spiral."""
    controller = ToolCallGuardrailController()
    for _ in range(4):
        controller.after_call("terminal", {"command": "x"}, '{"exit_code":1}', failed=True)
    # A success resets the same-tool failure count.
    controller.after_call("terminal", {"command": "x"}, '{"exit_code":0}', failed=False)
    # Four more failures should NOT reach the cap (streak restarted at 1-4).
    for _ in range(4):
        d = controller.after_call("terminal", {"command": "x"}, '{"exit_code":1}', failed=True)
        assert d.action != "halt"
    assert controller.halt_decision is None


def test_spiral_cap_reset_for_turn_clears_streak():
    """Per-turn reset clears the halt decision but NOT the cross-turn streak.

    The cross-turn count persists so one-failing-call-per-turn spirals
    accumulate (#1109–#1112).  After reset, halt_decision is cleared, but
    the next before_call for the same spiral-prone tool is blocked because
    the cross-turn streak already reached the cap.
    """
    controller = ToolCallGuardrailController()
    for _ in range(5):
        controller.after_call("terminal", {"command": "x"}, '{"exit_code":1}', failed=True)
    assert controller.halt_decision is not None
    controller.reset_for_turn()
    assert controller.halt_decision is None
    # The cross-turn streak persists — before_call now blocks the tool.
    d = controller.before_call("terminal", {"command": "x"})
    assert d.action == "block"
    assert d.code == "spiral_prone_tool_failure_cap"
    # A success after reset clears the cross-turn streak.
    controller.reset_for_turn()
    controller.before_call("terminal", {"command": "x"})
    d_ok = controller.after_call("terminal", {"command": "x"}, '{"exit_code":0}', failed=False)
    assert d_ok.action == "allow"
    controller.reset_for_turn()
    assert controller.before_call("terminal", {"command": "x"}).action == "allow"


def test_spiral_cap_does_not_affect_non_spiral_tools():
    """The spiral cap only applies to spiral-prone tools (terminal,
    execute_code, read_file) — not to write_file, patch, etc."""
    controller = ToolCallGuardrailController()
    last = None
    for _ in range(10):
        last = controller.after_call(
            "write_file", {"path": "x"}, '{"error":"boom"}', failed=True
        )
    assert last is not None
    assert last.action != "halt"
    assert controller.halt_decision is None


def test_spiral_cap_parsed_from_mapping():
    cfg = ToolCallGuardrailConfig.from_mapping({"spiral_failure_cap": 7})
    assert cfg.spiral_failure_cap == 7
    # 0 is honoured (disables); negative falls back to default.
    assert ToolCallGuardrailConfig.from_mapping({"spiral_failure_cap": 0}).spiral_failure_cap == 0
    assert ToolCallGuardrailConfig.from_mapping({"spiral_failure_cap": -3}).spiral_failure_cap == 5
    assert ToolCallGuardrailConfig.from_mapping({}).spiral_failure_cap == 5


def test_spiral_cap_default_is_5():
    """The default spiral cap is 5 — high enough to allow reasonable retries
    but low enough to stop the 55-1237-consecutive-retry spirals seen in
    the trace data."""
    cfg = ToolCallGuardrailConfig()
    assert cfg.spiral_failure_cap == 5


def test_spiral_prone_tools_set():
    """The spiral-prone set contains exactly the three tools with the highest
    trace-miner failure frequency."""
    cfg = ToolCallGuardrailConfig()
    assert "terminal" in cfg.spiral_prone_tools
    assert "execute_code" in cfg.spiral_prone_tools
    assert "read_file" in cfg.spiral_prone_tools
    assert len(cfg.spiral_prone_tools) == 3


# ── Cross-turn spiral enforcement (#1109–#1112) ─────────────────────────────


def test_cross_turn_spiral_accumulates_across_resets():
    """One failing terminal call per turn accumulates across reset_for_turn
    calls and eventually triggers the spiral cap via the cross-turn counter."""
    controller = ToolCallGuardrailController()
    for _ in range(4):  # 4 turns, one failing call each
        controller.before_call("terminal", {"command": "x"})
        controller.after_call("terminal", {"command": "x"}, '{"exit_code":1}', failed=True)
        controller.reset_for_turn()
    # 4 failures: not yet at cap (5), before_call should still allow
    assert controller.before_call("terminal", {"command": "x"}).action == "allow"
    # 5th turn: one more failure reaches the cap
    controller.after_call("terminal", {"command": "x"}, '{"exit_code":1}', failed=True)
    assert controller.halt_decision is not None
    assert controller.halt_decision.code == "spiral_prone_tool_failure_cap"


def test_cross_turn_before_call_blocks_after_cap_reached():
    """After the cross-turn streak reaches the cap, before_call blocks the
    tool on the NEXT turn even though per-turn state was reset."""
    controller = ToolCallGuardrailController()
    for _ in range(5):
        controller.after_call("terminal", {"command": "x"}, '{"exit_code":1}', failed=True)
    controller.reset_for_turn()
    # Next turn: before_call must block, not allow
    d = controller.before_call("terminal", {"command": "x"})
    assert d.action == "block"
    assert d.code == "spiral_prone_tool_failure_cap"
    assert d.fallback_directive != ""


def test_cross_turn_success_clears_streak():
    """A successful call after failures clears the cross-turn streak so
    legitimate retry-after-fix work is not blocked."""
    controller = ToolCallGuardrailController()
    for _ in range(3):
        controller.after_call("terminal", {"command": "x"}, '{"exit_code":1}', failed=True)
    controller.reset_for_turn()
    # Success on next turn
    controller.before_call("terminal", {"command": "x"})
    controller.after_call("terminal", {"command": "x"}, '{"exit_code":0}', failed=False)
    controller.reset_for_turn()
    # Streak cleared — before_call allows
    assert controller.before_call("terminal", {"command": "x"}).action == "allow"


def test_cross_turn_browser_cap_blocks_after_reset():
    """Browser tool cross-turn streak persists across reset and blocks via
    before_call."""
    controller = ToolCallGuardrailController()
    for _ in range(3):
        controller.after_call("browser_navigate", {"url": "u"}, '{"error":"boom"}', failed=True)
    controller.reset_for_turn()
    d = controller.before_call("browser_navigate", {"url": "u"})
    assert d.action == "block"
    assert d.code == "browser_tool_failure_cap"
    assert d.fallback_directive != ""


def test_cross_turn_does_not_affect_non_spiral_tools():
    """The cross-turn enforcement only applies to spiral-prone and browser
    tools — not to write_file, patch, etc."""
    controller = ToolCallGuardrailController()
    for _ in range(10):
        controller.after_call("write_file", {"path": "x"}, '{"error":"boom"}', failed=True)
    controller.reset_for_turn()
    assert controller.before_call("write_file", {"path": "x"}).action == "allow"


def test_cross_turn_blocks_with_hard_stop_disabled():
    """Cross-turn enforcement fires even when hard_stop_enabled is False —
    the spiral and browser caps are always-on."""
    controller = ToolCallGuardrailController(
        ToolCallGuardrailConfig(hard_stop_enabled=False)
    )
    for _ in range(5):
        controller.after_call("execute_code", {"code": "x"}, '{"error":"boom"}', failed=True)
    controller.reset_for_turn()
    d = controller.before_call("execute_code", {"code": "x"})
    assert d.action == "block"
    assert d.code == "spiral_prone_tool_failure_cap"
