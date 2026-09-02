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
from agent.tool_guardrails import LoopCapConfig


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
    assert cfg.non_interactive_hard_stop_enabled is True
    assert cfg.exact_failure_warn_after == 2
    assert cfg.same_tool_failure_warn_after == 3
    assert cfg.no_progress_warn_after == 2
    assert cfg.exact_failure_block_after == 5
    assert cfg.same_tool_failure_halt_after == 8
    assert cfg.no_progress_block_after == 5
    assert cfg.browser_failure_cap == 3


def test_config_parses_nested_warn_and_hard_stop_thresholds():
    cfg = ToolCallGuardrailConfig.from_mapping({
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
    })

    assert cfg.warnings_enabled is False
    assert cfg.hard_stop_enabled is True
    assert cfg.exact_failure_warn_after == 3
    assert cfg.same_tool_failure_warn_after == 4
    assert cfg.no_progress_warn_after == 5
    assert cfg.exact_failure_block_after == 6
    assert cfg.same_tool_failure_halt_after == 7
    assert cfg.no_progress_block_after == 8


def test_gateway_platform_defaults_to_hard_stop_without_changing_interactive_defaults():
    interactive_configs = [
        ToolCallGuardrailConfig.from_mapping({}, platform=platform)
        for platform in ("cli", "tui", "desktop", "acp")
    ]
    telegram_cfg = ToolCallGuardrailConfig.from_mapping({}, platform="telegram")
    cron_cfg = ToolCallGuardrailConfig.from_mapping({}, platform="cron")

    assert all(cfg.hard_stop_enabled is False for cfg in interactive_configs)
    assert telegram_cfg.hard_stop_enabled is True
    assert cron_cfg.hard_stop_enabled is True


def test_non_interactive_hard_stop_can_be_disabled_explicitly():
    cfg = ToolCallGuardrailConfig.from_mapping(
        {"non_interactive_hard_stop_enabled": False},
        platform="telegram",
    )

    assert cfg.hard_stop_enabled is False
    assert cfg.non_interactive_hard_stop_enabled is False


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
        ToolCallGuardrailConfig(
            hard_stop_enabled=True,
            exact_failure_block_after=2,
            same_tool_failure_halt_after=99,
        )
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
        ToolCallGuardrailConfig(
            same_tool_failure_warn_after=2, same_tool_failure_halt_after=3
        )
    )

    first = controller.after_call(
        "terminal", {"command": "cmd-1"}, '{"exit_code":1}', failed=True
    )
    second = controller.after_call(
        "terminal", {"command": "cmd-2"}, '{"exit_code":1}', failed=True
    )
    third = controller.after_call(
        "terminal", {"command": "cmd-3"}, '{"exit_code":1}', failed=True
    )
    fourth = controller.after_call(
        "terminal", {"command": "cmd-4"}, '{"exit_code":1}', failed=True
    )

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

    first = controller.after_call(
        "web_search", {"query": "cmd-1"}, '{"error":1}', failed=True
    )
    assert first.action == "allow"
    second = controller.after_call(
        "web_search", {"query": "cmd-2"}, '{"error":1}', failed=True
    )
    assert second.action == "warn"
    assert second.code == "same_tool_failure_warning"
    third = controller.after_call(
        "web_search", {"query": "cmd-3"}, '{"error":1}', failed=True
    )
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
    assert (
        controller.after_call("read_file", args, result, failed=False).action == "allow"
    )
    assert controller.before_call("read_file", args).action == "allow"
    warn = controller.after_call("read_file", args, result, failed=False)
    assert warn.action == "warn"
    assert warn.code == "idempotent_no_progress_warning"

    blocked = controller.before_call("read_file", args)
    assert blocked.action == "block"
    assert blocked.code == "idempotent_no_progress_block"


def test_skill_read_tools_are_idempotent_and_block_repeated_identical_success_output():
    cases = [
        (
            "skill_view",
            {"name": "gui-agent-ml-operations"},
            '{"success":true,"name":"gui-agent-ml-operations","content":"same"}',
        ),
        (
            "skills_list",
            {"category": "mlops"},
            '{"success":true,"skills":[{"name":"gui-agent-ml-operations"}]}',
        ),
    ]

    for tool_name, args, result in cases:
        controller = ToolCallGuardrailController(
            ToolCallGuardrailConfig(
                hard_stop_enabled=True,
                no_progress_warn_after=2,
                no_progress_block_after=2,
            )
        )

        assert controller.before_call(tool_name, args).action == "allow"
        assert controller.after_call(tool_name, args, result, failed=False).action == "allow"
        assert controller.before_call(tool_name, args).action == "allow"
        warn = controller.after_call(tool_name, args, result, failed=False)
        assert warn.action == "warn"
        assert warn.code == "idempotent_no_progress_warning"

        blocked = controller.before_call(tool_name, args)
        assert blocked.action == "block"
        assert blocked.code == "idempotent_no_progress_block"


def test_mutating_or_unknown_tools_are_not_blocked_for_repeated_identical_success_output_by_default():
    controller = ToolCallGuardrailController(
        ToolCallGuardrailConfig(no_progress_warn_after=2, no_progress_block_after=2)
    )

    for _ in range(3):
        assert (
            controller.before_call(
                "write_file", {"path": "/tmp/x", "content": "x"}
            ).action
            == "allow"
        )
        assert (
            controller.after_call(
                "write_file", {"path": "/tmp/x", "content": "x"}, "ok", failed=False
            ).action
            == "allow"
        )
        assert controller.before_call("custom_tool", {"x": 1}).action == "allow"
        assert (
            controller.after_call("custom_tool", {"x": 1}, "ok", failed=False).action
            == "allow"
        )


def test_reset_for_turn_clears_bounded_guardrail_state():
    controller = ToolCallGuardrailController(
        ToolCallGuardrailConfig(
            hard_stop_enabled=True,
            exact_failure_block_after=2,
            no_progress_block_after=2,
        )
    )
    controller.after_call(
        "web_search", {"query": "same"}, '{"error":"boom"}', failed=True
    )
    controller.after_call(
        "web_search", {"query": "same"}, '{"error":"boom"}', failed=True
    )
    controller.after_call("read_file", {"path": "/tmp/x"}, "same", failed=False)
    controller.after_call("read_file", {"path": "/tmp/x"}, "same", failed=False)

    assert controller.before_call("web_search", {"query": "same"}).action == "block"
    assert controller.before_call("read_file", {"path": "/tmp/x"}).action == "block"

    controller.reset_for_turn()

    assert controller.before_call("web_search", {"query": "same"}).action == "allow"
    assert controller.before_call("read_file", {"path": "/tmp/x"}).action == "allow"


def test_identical_call_streak_halts_any_tool_when_hard_stop_enabled():
    # #89069 / #100849 bundle: a model replaying the same SUCCESSFUL
    # terminal/skill_view call with a byte-identical result is not covered by
    # the idempotent_tools no-progress block. The consecutive-identical
    # streak (observe_call) is tool-agnostic; under hard_stop it must halt.
    controller = ToolCallGuardrailController(
        ToolCallGuardrailConfig(hard_stop_enabled=True, no_progress_block_after=5)
    )
    args = {"command": "hermes config get memory.provider"}
    for i in range(1, 5):
        controller.after_call("terminal", args, "local\n", failed=False)
        controller.observe_call("terminal", args, "local\n", failed=False)
        assert controller.halt_decision is None, f"halted early at {i}"

    controller.after_call("terminal", args, "local\n", failed=False)
    controller.observe_call("terminal", args, "local\n", failed=False)
    halt = controller.halt_decision
    assert halt is not None and halt.should_halt
    assert halt.code == "identical_call_streak_halt"
    assert halt.tool_name == "terminal" and halt.count == 5


def test_identical_call_streak_never_halts_when_hard_stop_disabled_or_for_pollers():
    soft = ToolCallGuardrailController(
        ToolCallGuardrailConfig(hard_stop_enabled=False, no_progress_block_after=2)
    )
    for _ in range(6):
        soft.observe_call("terminal", {"command": "ls"}, "a\nb\n", failed=False)
    assert soft.halt_decision is None  # notice-only in interactive sessions

    hard = ToolCallGuardrailController(
        ToolCallGuardrailConfig(hard_stop_enabled=True, no_progress_block_after=2)
    )
    for _ in range(6):
        hard.observe_call("process_manage", {"action": "poll", "session_id": "p1"}, "running", failed=False)
    assert hard.halt_decision is None  # an unchanged poll is legitimate progress

    # A changed result resets the streak.
    for i in range(6):
        hard.observe_call("terminal", {"command": "date"}, f"t{i}", failed=False)
    assert hard.halt_decision is None


# ── #744/#785: fallback_directive field on ToolGuardrailDecision ──────────────


def test_fallback_directive_populated_on_same_tool_failure_warning():
    """A repeated same-tool failure warning carries a non-empty fallback_directive."""
    controller = ToolCallGuardrailController()
    args = {"path": "/nonexistent"}
    # read_file is idempotent (fail_threshold for same_tool = 3 by default)
    for _ in range(3):
        controller.before_call("read_file", args)
        decision = controller.after_call(
            "read_file", args, '{"error":"not found"}', failed=True
        )
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
        decision = controller.after_call(
            "web_search", args, '{"error":"boom"}', failed=True
        )
    assert decision.action == "warn"
    assert decision.fallback_directive != ""
    assert "web_extract" in decision.fallback_directive


def test_fallback_directive_empty_on_allow():
    """A non-failure (allow) decision has an empty fallback_directive."""
    controller = ToolCallGuardrailController()
    controller.before_call("read_file", {"path": "/tmp/x"})
    decision = controller.after_call(
        "read_file", {"path": "/tmp/x"}, "content", failed=False
    )
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
        decision = controller.after_call(
            "mcp_custom_tool", args, '{"error":"bad"}', failed=True
        )
    assert decision.action == "warn"
    assert decision.fallback_directive == ""


def test_fallback_directive_in_metadata():
    """to_metadata() includes fallback_directive when non-empty, omits when empty."""
    controller = ToolCallGuardrailController()
    args = {"path": "/nonexistent"}
    for _ in range(3):
        controller.before_call("read_file", args)
        decision = controller.after_call(
            "read_file", args, '{"error":"not found"}', failed=True
        )
    assert decision.fallback_directive != ""
    meta = decision.to_metadata()
    assert "fallback_directive" in meta
    assert meta["fallback_directive"] == decision.fallback_directive

    # Allow decisions omit the key entirely
    controller.before_call("read_file", {"path": "/tmp/other"})
    allow_decision = controller.after_call(
        "read_file", {"path": "/tmp/other"}, "ok", failed=False
    )
    assert "fallback_directive" not in allow_decision.to_metadata()


# ── #739: media-tool fallback directives ──────────────────────────────────────


def test_fallback_directive_populated_for_vision_analyze():
    """Repeated vision_analyze failures carry a media-aware fallback_directive."""
    controller = ToolCallGuardrailController()
    args = {"path": "/bad.png"}
    for _ in range(3):
        controller.before_call("vision_analyze", args)
        decision = controller.after_call(
            "vision_analyze",
            args,
            '{"success": false, "error": "invalid image"}',
            failed=True,
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
            "image_generate",
            args,
            '{"success": false, "error": "provider error"}',
            failed=True,
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
    first = controller.after_call(
        "browser_navigate", {"url": "u"}, '{"error":"boom"}', failed=True
    )
    second = controller.after_call(
        "browser_navigate", {"url": "u"}, '{"error":"boom"}', failed=True
    )
    assert first.action == "allow"
    assert second.action == "warn"  # exact_failure_warn_after == 2
    assert controller.halt_decision is None


def test_browser_cap_can_be_disabled():
    """browser_failure_cap=0 disables the browser cap; spirals then follow the
    generic same-tool behaviour (warn-only when hard_stop is off)."""
    controller = ToolCallGuardrailController(
        ToolCallGuardrailConfig(browser_failure_cap=0)
    )
    decisions = [
        controller.after_call(
            "browser_navigate", {"url": f"u{i}"}, '{"error":"boom"}', failed=True
        )
        for i in range(6)
    ]
    assert all(d.action != "halt" for d in decisions)
    assert controller.halt_decision is None


def test_bot_detection_warning_classified_as_failure():
    """#1188 — browser_navigate returns success=True with bot_detection_warning
    when it lands on a Cloudflare/captcha page.  _detect_tool_failure must
    classify this as a failure so the guardrail cap can fire."""
    from agent.display import _detect_tool_failure

    result = '{"success": true, "url": "https://x", "title": "Just a moment...", "bot_detection_warning": "blocked"}'
    is_error, suffix = _detect_tool_failure("browser_navigate", result)
    assert is_error
    assert "bot detection" in suffix


def test_bot_detection_spiral_halts_at_cap():
    """#1188 — consecutive bot-detection 'successes' must hit the cap at 3,
    not spiral to 15 like the regression."""
    from agent.display import _detect_tool_failure

    controller = ToolCallGuardrailController()
    halted = False
    for i in range(5):
        bc = controller.before_call("browser_navigate", {"url": "https://x"})
        if not bc.allows_execution:
            halted = True
            break
        result = '{"success": true, "bot_detection_warning": "blocked"}'
        is_error, _ = _detect_tool_failure("browser_navigate", result)
        decision = controller.after_call(
            "browser_navigate", {"url": "https://x"}, result, failed=is_error
        )
        if decision.should_halt:
            halted = True
            break
        controller.reset_for_turn()
    assert halted


def test_browser_cap_leaves_native_tool_hard_stop_semantics_unchanged():
    """The always-on browser cap must not leak into non-spiral-prone native
    tools: with hard_stop OFF, a non-spiral same-tool failure spiral still
    only warns (never halts). Note: write_file is now spiral-prone (see #1840),
    so we use web_search here."""
    controller = ToolCallGuardrailController()  # hard_stop off
    decisions = [
        controller.after_call(
            "web_search", {"query": f"q-{i}"}, '{"error":"boom"}', failed=True
        )
        for i in range(10)
    ]
    assert all(d.action != "halt" for d in decisions)
    assert controller.halt_decision is None


def test_browser_cap_success_decays_streak():
    """A successful browser call decays (not resets) the cross-turn failure
    streak by 1 (#1188).  Two failures + one success + two more failures
    still reaches the cap because the streak decayed 2→1→2→3, not 2→0→1→2.
    Multiple consecutive successes drain the streak back to 0 so a
    genuinely recovered backend is not penalized."""
    controller = ToolCallGuardrailController()
    controller.after_call(
        "browser_navigate", {"url": "u"}, '{"error":"boom"}', failed=True
    )
    controller.after_call(
        "browser_navigate", {"url": "u"}, '{"error":"boom"}', failed=True
    )
    # A success decays the cross-turn streak by 1 (2→1), not resets to 0.
    controller.after_call(
        "browser_navigate", {"url": "u"}, '{"success": true}', failed=False
    )
    # Two more failures: streak goes 1→2→3, so the second one hits the cap.
    d1 = controller.after_call(
        "browser_navigate", {"url": "u"}, '{"error":"boom"}', failed=True
    )
    d2 = controller.after_call(
        "browser_navigate", {"url": "u"}, '{"error":"boom"}', failed=True
    )
    assert d1.action != "halt"
    assert d2.action == "halt"
    assert d2.code == "browser_tool_failure_cap"
    # The cap fires at count=3 (the decayed streak: 2→1→2→3).
    assert d2.count == 3


def test_browser_cap_consecutive_successes_drain_streak():
    """Multiple consecutive successes drain the cross-turn streak to 0 so a
    genuinely recovered browser backend is not penalized (#1188)."""
    controller = ToolCallGuardrailController()
    # Two failures → streak=2
    controller.after_call(
        "browser_navigate", {"url": "u"}, '{"error":"boom"}', failed=True
    )
    controller.after_call(
        "browser_navigate", {"url": "u"}, '{"error":"boom"}', failed=True
    )
    assert controller._cross_turn_tool_failure_counts.get("browser_navigate", 0) == 2
    # Two successes → streak decays 2→1→0 (removed from dict)
    controller.after_call(
        "browser_navigate", {"url": "u"}, '{"success": true}', failed=False
    )
    assert controller._cross_turn_tool_failure_counts.get("browser_navigate", 0) == 1
    controller.after_call(
        "browser_navigate", {"url": "u"}, '{"success": true}', failed=False
    )
    assert controller._cross_turn_tool_failure_counts.get("browser_navigate", 0) == 0
    # A subsequent failure starts a fresh streak at 1, not continuing from 0.
    d = controller.after_call(
        "browser_navigate", {"url": "u"}, '{"error":"boom"}', failed=True
    )
    assert d.action != "halt"


def test_browser_cap_reset_for_turn_clears_streak():
    """Per-turn reset clears the halt decision but NOT the cross-turn streak.

    #1826 — browser tools reaching the cap are also session-hard-stopped, so
    the before_call code stays "browser_tool_failure_cap" and successes cannot
    drain the streak (the permanent stop is irreversible).
    """
    controller = ToolCallGuardrailController()
    for _ in range(3):
        controller.after_call(
            "browser_navigate", {"url": "u"}, '{"error":"boom"}', failed=True
        )
    assert controller.halt_decision is not None
    assert "browser_navigate" in controller._session_hard_stopped
    controller.reset_for_turn()
    assert controller.halt_decision is None
    # The cross-turn streak persists — before_call now blocks the browser tool.
    d = controller.before_call("browser_navigate", {"url": "u"})
    assert d.action == "block"
    assert d.code == "browser_tool_failure_cap"
    # #1826 — session stop is permanent; successes cannot drain it.
    controller.reset_for_turn()
    controller.after_call("browser_navigate", {"url": "u"}, '{"ok":true}', failed=False)
    controller.reset_for_turn()
    assert controller.before_call("browser_navigate", {"url": "u"}).action == "block"


def test_browser_failure_cap_parsed_from_mapping():
    cfg = ToolCallGuardrailConfig.from_mapping({"browser_failure_cap": 5})
    assert cfg.browser_failure_cap == 5
    # 0 is honoured (disables); negative falls back to default.
    assert (
        ToolCallGuardrailConfig.from_mapping({
            "browser_failure_cap": 0
        }).browser_failure_cap
        == 0
    )
    assert (
        ToolCallGuardrailConfig.from_mapping({
            "browser_failure_cap": -3
        }).browser_failure_cap
        == 3
    )
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
    assert (
        "read_file" in halt.fallback_directive
        or "diagnostic" in halt.fallback_directive
    )
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
    """read_file failures hit the per-tool exploration cap (default 10)."""
    controller = ToolCallGuardrailController()
    cap = controller._effective_cap_for("read_file")
    assert cap == 10
    decisions = []
    for i in range(cap + 1):
        decisions.append(
            controller.after_call(
                "read_file",
                {"path": f"/nonexistent/{i}"},
                '{"error": "File not found"}',
                failed=True,
            )
        )
    halt = decisions[cap - 1]
    assert halt.action == "halt"
    assert halt.code == "spiral_prone_tool_failure_cap"
    assert halt.tool_name == "read_file"
    assert halt.fallback_directive != ""


def test_read_file_single_success_decays_streak():
    """Read-only tools decay the failure streak after one success."""
    controller = ToolCallGuardrailController()
    cap = controller._effective_cap_for("read_file")
    for _ in range(3):
        controller.after_call(
            "read_file", {"path": "/x"}, '{"error":"missing"}', failed=True
        )
    assert controller._cross_turn_tool_failure_counts.get("read_file") == 3
    controller.after_call(
        "read_file", {"path": "/x"}, '{"content":"ok"}', failed=False
    )
    assert controller._cross_turn_tool_failure_counts.get("read_file") == 2
    assert cap == 10


def test_spiral_cap_does_not_fire_before_threshold():
    """Below the cap, spiral-prone tool failures only warn — the cap does not
    over-trigger.  Uses the same command twice so exact_failure_warn_after (2)
    fires on the second call, matching the browser cap test pattern."""
    controller = ToolCallGuardrailController()
    first = controller.after_call(
        "terminal", {"command": "same"}, '{"exit_code":1}', failed=True
    )
    second = controller.after_call(
        "terminal", {"command": "same"}, '{"exit_code":1}', failed=True
    )
    assert first.action == "allow"
    assert second.action == "warn"  # exact_failure_warn_after == 2
    assert controller.halt_decision is None


def test_spiral_cap_can_be_disabled():
    """spiral_failure_cap=0 disables the cap; spirals then follow the generic
    same-tool behaviour (warn-only when hard_stop is off)."""
    controller = ToolCallGuardrailController(
        ToolCallGuardrailConfig(spiral_failure_cap=0)
    )
    decisions = [
        controller.after_call(
            "terminal", {"command": f"cmd-{i}"}, '{"exit_code":1}', failed=True
        )
        for i in range(10)
    ]
    assert all(d.action != "halt" for d in decisions)
    assert controller.halt_decision is None


def test_spiral_cap_success_decays_streak():
    """A successful terminal call decays (not resets) the cross-turn failure
    streak (#1188).  With spiral_failure_cap=5, four failures + one
    success leaves streak=4; the 5th failure (streak 4→5) hits the cap.
    Multiple consecutive successes drain the streak fully.

    #1585 — spiral-prone tools now require _SUCCESSES_TO_DECAY (2)
    consecutive successes before draining the streak by 1, so a single
    interspersed success (the pwd/ls diagnostic the directive recommends)
    does not reset the accumulation."""
    from agent.tool_guardrails import _SUCCESSES_TO_DECAY

    controller = ToolCallGuardrailController()
    cap = controller.config.spiral_failure_cap  # default 5
    for _ in range(4):
        controller.after_call(
            "terminal", {"command": "x"}, '{"exit_code":1}', failed=True
        )
    # Cross-turn streak is now 4.
    assert controller._cross_turn_tool_failure_counts.get("terminal", 0) == 4
    # A SINGLE success does NOT decay (needs _SUCCESSES_TO_DECAY in a row).
    controller.after_call("terminal", {"command": "x"}, '{"exit_code":0}', failed=False)
    assert controller._cross_turn_tool_failure_counts.get("terminal", 0) == 4
    # A second consecutive success drains by 1 (4→3).
    controller.after_call("terminal", {"command": "x"}, '{"exit_code":0}', failed=False)
    assert controller._cross_turn_tool_failure_counts.get("terminal", 0) == 3
    # Two more failures bring the streak to 5, hitting the cap.
    controller.after_call("terminal", {"command": "x"}, '{"exit_code":1}', failed=True)
    assert controller._cross_turn_tool_failure_counts.get("terminal", 0) == 4
    d = controller.after_call(
        "terminal", {"command": "x"}, '{"exit_code":1}', failed=True
    )
    assert d.action == "halt"
    assert d.code == "spiral_prone_tool_failure_cap"
    # #1826 — once the cap is hit, the tool is session-hard-stopped. Successes
    # can no longer drain the streak (the permanent stop is irreversible).
    assert "terminal" in controller._session_hard_stopped
    controller2 = ToolCallGuardrailController()
    for _ in range(cap):
        controller2.after_call(
            "terminal", {"command": "x"}, '{"exit_code":1}', failed=True
        )
    assert controller2._cross_turn_tool_failure_counts.get("terminal", 0) == cap
    assert "terminal" in controller2._session_hard_stopped
    # Successes do NOT drain because the session stop is permanent.
    for _ in range(cap * _SUCCESSES_TO_DECAY):
        controller2.after_call(
            "terminal", {"command": "x"}, '{"exit_code":0}', failed=False
        )
    assert controller2._cross_turn_tool_failure_counts.get("terminal", 0) == cap


def test_spiral_cap_reset_for_turn_clears_streak():
    """Per-turn reset clears the halt decision but NOT the cross-turn streak.

    The cross-turn count persists so one-failing-call-per-turn spirals
    accumulate (#1109–#1112).  After reset, halt_decision is cleared, but
    the next before_call for the same spiral-prone tool is blocked because
    the cross-turn streak already reached the cap.

    #1826 — once the cap is hit the tool is permanently session-hard-stopped.
    The before_call code changes to "session_hard_stop" and successes CANNOT
    drain the streak (the permanent stop is irreversible).
    """
    controller = ToolCallGuardrailController()
    for _ in range(5):
        controller.after_call(
            "terminal", {"command": "x"}, '{"exit_code":1}', failed=True
        )
    assert controller.halt_decision is not None
    assert "terminal" in controller._session_hard_stopped
    controller.reset_for_turn()
    assert controller.halt_decision is None
    # The cross-turn streak persists — before_call now blocks the tool with
    # the session_hard_stop code (it was permanently stopped when the cap hit).
    d = controller.before_call("terminal", {"command": "x"})
    assert d.action == "block"
    assert d.code == "session_hard_stop"
    # Successes do NOT clear the session stop — it's permanent.
    from agent.tool_guardrails import _SUCCESSES_TO_DECAY

    controller.reset_for_turn()
    for _ in range(5 * _SUCCESSES_TO_DECAY):
        d_ok = controller.after_call(
            "terminal", {"command": "x"}, '{"exit_code":0}', failed=False
        )
        assert d_ok.action in ("allow", "warn")
        controller.reset_for_turn()
    # Still blocked — the session stop cannot be drained.
    assert controller.before_call("terminal", {"command": "x"}).action == "block"


def test_spiral_cap_does_not_affect_non_spiral_tools():
    """The spiral cap only applies to spiral-prone tools (terminal,
    execute_code, read_file, patch, write_file) — not to web_search, etc."""
    controller = ToolCallGuardrailController()
    last = None
    for _ in range(10):
        last = controller.after_call(
            "web_search", {"query": "x"}, '{"error":"boom"}', failed=True
        )
    assert last is not None
    assert last.action != "halt"
    assert controller.halt_decision is None


def test_spiral_cap_parsed_from_mapping():
    cfg = ToolCallGuardrailConfig.from_mapping({"spiral_failure_cap": 7})
    assert cfg.spiral_failure_cap == 7
    # 0 is honoured (disables); negative falls back to default.
    assert (
        ToolCallGuardrailConfig.from_mapping({
            "spiral_failure_cap": 0
        }).spiral_failure_cap
        == 0
    )
    assert (
        ToolCallGuardrailConfig.from_mapping({
            "spiral_failure_cap": -3
        }).spiral_failure_cap
        == 5
    )
    assert ToolCallGuardrailConfig.from_mapping({}).spiral_failure_cap == 5


def test_spiral_cap_default_is_5():
    """The default spiral cap is 5 — high enough to allow reasonable retries
    but low enough to stop the 55-1237-consecutive-retry spirals seen in
    the trace data."""
    cfg = ToolCallGuardrailConfig()
    assert cfg.spiral_failure_cap == 5


def test_spiral_prone_tools_set():
    """The spiral-prone set contains the tools with the highest trace-miner
    failure frequency. Membership is the invariant; the set grows as new
    spiral-prone tools are identified (#1141 added process, #1143 added
    search_files)."""
    cfg = ToolCallGuardrailConfig()
    assert "terminal" in cfg.spiral_prone_tools
    assert "execute_code" in cfg.spiral_prone_tools
    assert "read_file" in cfg.spiral_prone_tools
    assert "process" in cfg.spiral_prone_tools
    assert "search_files" in cfg.spiral_prone_tools
    assert len(cfg.spiral_prone_tools) >= 5


# ── Cross-turn spiral enforcement (#1109–#1112) ─────────────────────────────


def test_cross_turn_spiral_accumulates_across_resets():
    """One failing terminal call per turn accumulates across reset_for_turn
    calls and eventually triggers the spiral cap via the cross-turn counter."""
    controller = ToolCallGuardrailController()
    for _ in range(4):  # 4 turns, one failing call each
        controller.before_call("terminal", {"command": "x"})
        controller.after_call(
            "terminal", {"command": "x"}, '{"exit_code":1}', failed=True
        )
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
        controller.after_call(
            "terminal", {"command": "x"}, '{"exit_code":1}', failed=True
        )
    controller.reset_for_turn()
    # Next turn: before_call must block, not allow.
    d = controller.before_call("terminal", {"command": "x"})
    assert d.action == "block"
    # #1826 — once session-hard-stopped, before_call uses the session code.
    assert d.code == "session_hard_stop"
    assert d.fallback_directive != ""


def test_cross_turn_success_clears_streak():
    """A successful call after failures clears the cross-turn streak so
    legitimate retry-after-fix work is not blocked."""
    controller = ToolCallGuardrailController()
    for _ in range(3):
        controller.after_call(
            "terminal", {"command": "x"}, '{"exit_code":1}', failed=True
        )
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
        controller.after_call(
            "browser_navigate", {"url": "u"}, '{"error":"boom"}', failed=True
        )
    controller.reset_for_turn()
    d = controller.before_call("browser_navigate", {"url": "u"})
    assert d.action == "block"
    assert d.code == "browser_tool_failure_cap"
    assert d.fallback_directive != ""


def test_cross_turn_does_not_affect_non_spiral_tools():
    """The cross-turn enforcement only applies to spiral-prone and browser
    tools — not to web_search, etc."""
    controller = ToolCallGuardrailController()
    for _ in range(10):
        controller.after_call(
            "web_search", {"query": "x"}, '{"error":"boom"}', failed=True
        )
    controller.reset_for_turn()
    assert controller.before_call("web_search", {"query": "x"}).action == "allow"


def test_cross_turn_process_spiral_cap_fires():
    """#1141 — process is now in _SPIRAL_PRONE_TOOLS so a cross-turn process
    failure streak should trigger the spiral-prone cap, not run uncapped."""
    controller = ToolCallGuardrailController()
    for _ in range(5):
        controller.after_call(
            "process", {"action": "poll"}, '{"error":"timeout"}', failed=True
        )
    controller.reset_for_turn()
    d = controller.before_call("process", {"action": "poll"})
    assert d.action == "block"
    # #1826 — session-hard-stopped tools use session_hard_stop code.
    assert d.code == "session_hard_stop"
    assert d.fallback_directive != ""


def test_cross_turn_search_files_spiral_cap_fires():
    """#1143 — search_files is now in _SPIRAL_PRONE_TOOLS so a cross-turn
    search_files failure streak should trigger the spiral-prone cap,
    not run uncapped as it did when 27 consecutive / 224 sessions regressed."""
    controller = ToolCallGuardrailController()
    cap = controller._effective_cap_for("search_files")
    assert cap == 10
    for _ in range(cap):
        controller.after_call(
            "search_files",
            {"pattern": "*.py", "target": "content"},
            '{"error":"no matches"}',
            failed=True,
        )
    controller.reset_for_turn()
    d = controller.before_call("search_files", {"pattern": "*.py", "target": "content"})
    assert d.action == "block"
    # #1826 — session-hard-stopped tools use session_hard_stop code.
    assert d.code == "session_hard_stop"
    assert d.fallback_directive != ""
    assert "target=files" in d.fallback_directive


def test_search_files_fallback_directive_includes_strategy_switch():
    """#1143 — the search_files fallback directive should guide the agent to
    switch strategy (files mode, broader path, glob vs regex) rather than
    just saying 'try a broader glob pattern'."""
    from agent.tool_guardrails import _fallback_directive_for

    directive = _fallback_directive_for("search_files")
    assert "target=files" in directive
    assert "broaden" in directive.lower()


def test_cross_turn_blocks_with_hard_stop_disabled():
    """Cross-turn enforcement fires even when hard_stop_enabled is False —
    the spiral and browser caps are always-on."""
    controller = ToolCallGuardrailController(
        ToolCallGuardrailConfig(hard_stop_enabled=False)
    )
    for _ in range(5):
        controller.after_call(
            "execute_code", {"code": "x"}, '{"error":"boom"}', failed=True
        )
    controller.reset_for_turn()
    d = controller.before_call("execute_code", {"code": "x"})
    assert d.action == "block"
    # #1826 — session-hard-stopped tools use session_hard_stop code.
    assert d.code == "session_hard_stop"


def test_cross_turn_tool_call_spiral_cap_fires():
    """#1185 — tool_call is now in _SPIRAL_PRONE_TOOLS so a cross-turn
    tool_call failure streak should trigger the spiral-prone cap, not run
    uncapped as it did when 168 failures / 21 sessions / 13-deep spirals
    regressed. The deferred-tool invocation chain had no circuit breaker."""
    controller = ToolCallGuardrailController()
    for _ in range(5):
        controller.after_call(
            "tool_call",
            {"name": "some_mcp_tool", "arguments": "{}"},
            '{"error":"tool unavailable"}',
            failed=True,
        )
    controller.reset_for_turn()
    d = controller.before_call(
        "tool_call", {"name": "some_mcp_tool", "arguments": "{}"}
    )
    assert d.action == "block"
    # #1826 — session-hard-stopped tools use session_hard_stop code.
    assert d.code == "session_hard_stop"
    assert d.fallback_directive != ""
    assert "tool_search" in d.fallback_directive


def test_cross_turn_memory_spiral_cap_fires():
    """#1186/#1825 — memory is in _SPIRAL_PRONE_TOOLS with a LOWER per-tool
    cap (default 3 instead of 5). A cross-turn memory failure streak should
    trigger the cap at 3, not 5. Once triggered, the tool is session-hard-
    stopped (#1826) so before_call uses the session code."""
    controller = ToolCallGuardrailController()
    # #1825 — memory cap is 3, not 5. Three failures should trigger the cap.
    for _ in range(3):
        controller.after_call(
            "memory",
            {"action": "store", "content": "x"},
            '{"error":"store locked"}',
            failed=True,
        )
    assert "memory" in controller._session_hard_stopped
    controller.reset_for_turn()
    d = controller.before_call("memory", {"action": "store", "content": "x"})
    assert d.action == "block"
    # #1826 — session-hard-stopped tools use session_hard_stop code.
    assert d.code == "session_hard_stop"
    assert d.fallback_directive != ""


def test_cross_turn_tool_describe_spiral_cap_fires():
    """#1187 — tool_describe is now in _SPIRAL_PRONE_TOOLS so a cross-turn
    tool_describe failure streak should trigger the spiral-prone cap. This
    is the middle step of the search→describe→call deferred-tool chain; when
    it fails 59 times the agent had no schema to invoke deferred tools and
    no breaker to stop the spiral."""
    controller = ToolCallGuardrailController()
    for _ in range(5):
        controller.after_call(
            "tool_describe",
            {"name": "stale_tool_name"},
            '{"error":"not in catalog"}',
            failed=True,
        )
    controller.reset_for_turn()
    d = controller.before_call("tool_describe", {"name": "stale_tool_name"})
    assert d.action == "block"
    # #1826 — session-hard-stopped tools use session_hard_stop code.
    assert d.code == "session_hard_stop"
    assert d.fallback_directive != ""
    assert "tool_search" in d.fallback_directive


def test_spiral_prone_tools_includes_deferred_tool_chain_and_memory():
    """#1185/#1186/#1187 — tool_call, tool_describe, and memory are now in
    _SPIRAL_PRONE_TOOLS alongside the original five, covering the whole
    deferred-tool loading chain (search→describe→call) plus memory."""
    cfg = ToolCallGuardrailConfig()
    assert "terminal" in cfg.spiral_prone_tools
    assert "execute_code" in cfg.spiral_prone_tools
    assert "read_file" in cfg.spiral_prone_tools
    assert "process" in cfg.spiral_prone_tools
    assert "search_files" in cfg.spiral_prone_tools
    assert "tool_call" in cfg.spiral_prone_tools
    assert "tool_describe" in cfg.spiral_prone_tools
    assert "memory" in cfg.spiral_prone_tools
    assert len(cfg.spiral_prone_tools) >= 8


def test_tool_call_fallback_directive_routes_to_search():
    """#1185 — the tool_call fallback directive should route the agent to
    tool_search / tool_describe / a core tool, not 'retry the same call'."""
    from agent.tool_guardrails import _fallback_directive_for

    directive = _fallback_directive_for("tool_call")
    assert "tool_search" in directive
    assert "tool_describe" in directive


def test_tool_describe_fallback_directive_routes_to_search_refresh():
    """#1187 — the tool_describe fallback directive should tell the agent to
    re-run tool_search to refresh the catalog rather than re-describing the
    same failing name."""
    from agent.tool_guardrails import _fallback_directive_for

    directive = _fallback_directive_for("tool_describe")
    assert "tool_search" in directive


def test_memory_fallback_directive_distinguishes_transient_from_hard():
    """#1186 — the memory fallback directive should distinguish busy/locked
    (transient, retry once) from genuine failure (skip-and-continue)."""
    from agent.tool_guardrails import _fallback_directive_for

    directive = _fallback_directive_for("memory")
    assert "busy" in directive.lower() or "transient" in directive.lower()


def test_after_call_survives_lone_surrogates_in_result_and_args():
    # Scraped web/social text can contain unpaired UTF-16 surrogates (e.g. the
    # first half of a mathematical-bold pair, '\ud835'). str.encode('utf-8')
    # rejects them, and the result hasher crashed the whole conversation loop
    # (live outage: "Outer loop error in API call #34 ... surrogates not
    # allowed"). Weird text must never take down the loop.
    controller = ToolCallGuardrailController(
        ToolCallGuardrailConfig(
            hard_stop_enabled=True,
            exact_failure_block_after=2,
            no_progress_block_after=2,
        )
    )
    dirty = "price \ud835 update"

    decision = controller.after_call(
        "web_search", {"query": dirty}, dirty, failed=False
    )
    assert decision.action in {"allow", "warn"}

    # hashing stays deterministic: the same dirty failure twice still trips
    # the exact-failure guard, proving the hash is stable across calls
    controller.after_call(
        "web_search", {"query": dirty}, '{"error":"\ud835 boom"}', failed=True
    )
    controller.after_call(
        "web_search", {"query": dirty}, '{"error":"\ud835 boom"}', failed=True
    )
    assert controller.before_call("web_search", {"query": dirty}).action == "block"


# ── #1585: spiral-prone interleaving pattern ──────────────────────────────


def test_spiral_cap_fail_success_interleaving_accumulates():
    """#1585 — the production terminal spiral pattern is fail → diagnostic-
    success (pwd, ls) → fail → success → ..., repeating for 25+ turns. The
    fallback directive actively recommends the diagnostic, which succeeds
    (exit 0) and must NOT reset the failure streak. With the old code, the
    pop() at current<=1 dropped the streak 1→0 on every interspersed
    success, so it never climbed past 1 and the cap (5) was unreachable.

    After the fix, spiral-prone tools only decay by 1 per success (never
    pop at <=1), so the fail/succeed/fail pattern nets +1 per cycle and
    eventually halts.
    """
    controller = ToolCallGuardrailController()
    # Simulate fail → success per turn for 10 turns (20 calls).
    for turn in range(10):
        controller.reset_for_turn()
        # Failing terminal call (the real command).
        controller.after_call(
            "terminal", {"command": f"cmd{turn}"}, '{"exit_code":1}', failed=True
        )
        # Successful diagnostic (pwd, ls — the fallback directive's advice).
        controller.after_call(
            "terminal", {"command": "pwd"}, '{"exit_code":0}', failed=False
        )
    # After 10 fail/success cycles, the cross-turn streak should have
    # accumulated well beyond 1 (each cycle nets +1: fail +1, success -1).
    # With the old pop()-at-1 code, this was permanently stuck at 0.
    streak = controller._cross_turn_tool_failure_counts.get("terminal", 0)
    assert streak >= 5, (
        f"Interleaving spiral should accumulate to >=5 after 10 cycles, "
        f"got {streak} — the single-success reset bug (#1585) is still present"
    )


def test_spiral_cap_interleaving_eventually_halts():
    """The fail/success interleaving pattern must eventually hit the cap and
    halt — proving #1585 is fixed end-to-end."""
    controller = ToolCallGuardrailController()
    halted = False
    for turn in range(20):
        controller.reset_for_turn()
        d_fail = controller.after_call(
            "terminal",
            {"command": f"failing-cmd-{turn}"},
            '{"exit_code":1}',
            failed=True,
        )
        if d_fail.should_halt:
            halted = True
            break
        # Interspersed success between failures (the pattern that defeated
        # the old cap).
        controller.after_call(
            "terminal", {"command": "pwd"}, '{"exit_code":0}', failed=False
        )
    assert halted, (
        "Terminal fail/success interleaving should have halted within 20 "
        "turns — the #1585 spiral cap must fire for this pattern"
    )


def test_loop_cap_zero_disables_and_junk_falls_back():
    # 0 is a legitimate "unlimited" value; negatives / junk fall back to default.
    assert LoopCapConfig.from_mapping({"max_web_searches": 0}).max_web_searches == 0
    assert LoopCapConfig.from_mapping({"max_web_searches": -5}).max_web_searches == 50
    assert LoopCapConfig.from_mapping({"max_subagents": "nope"}).max_subagents == 50


def test_web_search_cap_blocks_after_limit_regardless_of_hard_stop():
    # Loop caps fire even with hard_stop_enabled=False (the per-turn loop
    # detector's flag). Each distinct query avoids the loop detector so we know
    # the block came from the loop cap, not exact-failure repetition.
    controller = ToolCallGuardrailController(
        ToolCallGuardrailConfig(
            hard_stop_enabled=False,
            loop_caps=LoopCapConfig(max_web_searches=3),
        )
    )
    for i in range(3):
        assert (
            controller.before_call("web_search", {"query": f"q{i}"}).action == "allow"
        )
    decision = controller.before_call("web_search", {"query": "q4"})
    assert decision.action == "block"
    assert decision.code == "loop_web_search_cap"
    assert decision.should_halt is True


# ── #1826/#1825: session-level permanent hard-stop + per-tool caps ──────────


def test_session_hard_stop_persists_across_turns():
    """#1826 — once a spiral-prone tool hits the cap, it is permanently
    session-hard-stopped. reset_for_turn does NOT clear the session stop,
    and the tool stays blocked for the rest of the session."""
    controller = ToolCallGuardrailController()
    for _ in range(5):
        controller.after_call(
            "terminal", {"command": "x"}, '{"exit_code":1}', failed=True
        )
    assert "terminal" in controller._session_hard_stopped
    # Multiple resets + turns — still blocked
    for _ in range(10):
        controller.reset_for_turn()
        d = controller.before_call("terminal", {"command": "x"})
        assert d.action == "block"
        assert d.code == "session_hard_stop"


def test_session_hard_stop_only_affects_capped_tool():
    """#1826 — session-hard-stopping terminal does not block write_file."""
    controller = ToolCallGuardrailController()
    for _ in range(5):
        controller.after_call(
            "terminal", {"command": "x"}, '{"exit_code":1}', failed=True
        )
    assert "terminal" in controller._session_hard_stopped
    assert "write_file" not in controller._session_hard_stopped
    controller.reset_for_turn()
    # write_file is still allowed even though terminal is session-stopped
    assert controller.before_call("write_file", {"path": "x"}).action == "allow"


def test_session_hard_stop_success_does_not_unblock():
    """#1826 — a successful call to a session-hard-stopped tool does NOT
    clear the stop. The streak is frozen — no decay, no recovery."""
    controller = ToolCallGuardrailController()
    for _ in range(5):
        controller.after_call(
            "terminal", {"command": "x"}, '{"exit_code":1}', failed=True
        )
    assert "terminal" in controller._session_hard_stopped
    # Inject a success — the streak should NOT decay
    controller.reset_for_turn()
    controller.after_call("terminal", {"command": "x"}, '{"exit_code":0}', failed=False)
    controller.reset_for_turn()
    # Still blocked
    d = controller.before_call("terminal", {"command": "x"})
    assert d.action == "block"
    assert d.code == "session_hard_stop"


def test_memory_session_hard_stop_at_lower_cap():
    """#1825 — memory tools get a lower per-tool cap (3, not 5) so the
    session-hard-stop fires sooner, matching the 11-deep spiral data."""
    controller = ToolCallGuardrailController()
    assert controller._effective_cap_for("memory") == 3
    # Only 3 failures needed to hit the memory cap
    for _ in range(3):
        controller.after_call(
            "memory",
            {"action": "store", "content": "x"},
            '{"error":"locked"}',
            failed=True,
        )
    assert "memory" in controller._session_hard_stopped


def test_per_tool_cap_from_config():
    """#1825 — per_tool_failure_caps can be parsed from config.yaml."""
    cfg = ToolCallGuardrailConfig.from_mapping({
        "per_tool_failure_caps": {"terminal": 2, "memory": 4}
    })
    assert cfg.per_tool_failure_caps["terminal"] == 2
    assert cfg.per_tool_failure_caps["memory"] == 4
    # Non-configured tools fall back to the default cap
    controller = ToolCallGuardrailController(cfg)
    assert controller._effective_cap_for("terminal") == 2
    assert controller._effective_cap_for("memory") == 4
    assert controller._effective_cap_for("execute_code") == 5  # default


def test_per_tool_cap_invalid_entries_ignored():
    """#1825 — invalid per-tool cap entries (non-int, negative) are dropped."""
    cfg = ToolCallGuardrailConfig.from_mapping({
        "per_tool_failure_caps": {"terminal": "oops", "memory": -1, "patch": 2}
    })
    # terminal: not parsed (string), falls back to default 5
    # memory: negative, not valid, falls back to default 3
    # patch: valid
    assert cfg.per_tool_failure_caps["memory"] == 3  # default not overridden
    assert cfg.per_tool_failure_caps["patch"] == 2


def test_memory_fallback_directive_includes_unavailable_directive():
    """#1825 — memory fallback directive tells agent to proceed without memory."""
    from agent.tool_guardrails import _fallback_directive_for

    directive = _fallback_directive_for("memory")
    assert "unavailable" in directive.lower()


# ── Legitimate flows must survive hard stops (Teknium, Sep 2026) ────────────
# Hard stops default ON for unattended platforms. These pin the flows that
# must NEVER be cut off there: edit -> re-run loops, diagnostic sweeps of
# distinct red commands, and browser retry-after-action — while the pure
# replay (same call, nothing changed between attempts) is still stopped.

_HARD = lambda: ToolCallGuardrailController(  # noqa: E731
    ToolCallGuardrailConfig(hard_stop_enabled=True)
)
_PYTEST = {"command": "pytest tests/test_x.py -q"}
_RED = '{"output": "1 failed", "exit_code": 1}'


def _run_red(c, args=_PYTEST):
    assert c.before_call("terminal", args).allows_execution
    return c.after_call("terminal", args, _RED, failed=True)


def test_fix_retest_loop_is_never_hard_stopped():
    c = _HARD()
    for i in range(12):
        d = _run_red(c)
        assert not d.should_halt, f"halted on red run {i + 1}"
        # the model edits between runs — a landed mutation is progress
        c.after_call("patch", {"path": "x.py", "old_string": "a", "new_string": f"b{i}"},
                     '{"success": true, "diff": "..."}', failed=False)
    assert c.halt_decision is None
    assert c.before_call("terminal", _PYTEST).allows_execution


def test_pure_replay_with_no_intervening_change_is_still_blocked():
    c = _HARD()
    for _ in range(5):
        _run_red(c)
    d = c.before_call("terminal", _PYTEST)
    assert d.action == "block" and d.code == "repeated_exact_failure_block"


def test_intervening_mutation_resets_the_replay_streak_only_once():
    # 4 reds, one edit, then 4 reds with NO edit: the second run of 4 is a
    # fresh streak, and the 5th unchanged retry after it is blocked.
    c = _HARD()
    for _ in range(4):
        _run_red(c)
    c.after_call("write_file", {"path": "x.py", "content": "y"}, '{"bytes_written": 1}', failed=False)
    for _ in range(5):
        assert c.before_call("terminal", _PYTEST).allows_execution
        c.after_call("terminal", _PYTEST, _RED, failed=True)
    assert c.before_call("terminal", _PYTEST).action == "block"


def test_distinct_failing_terminal_commands_warn_but_never_halt():
    # A diagnostic sweep: grep with no matches, missing binaries, red builds.
    c = _HARD()
    for i in range(12):
        args = {"command": f"grep -q needle{i} haystack.txt"}
        d = c.after_call("terminal", args, _RED, failed=True)
        assert not d.should_halt, f"same_tool halt on distinct command #{i + 1}"
    assert c.halt_decision is None
    # ...while a non-tolerant tool failing 8 distinct ways still halts.
    c2 = _HARD()
    last = None
    for i in range(8):
        last = c2.after_call("send_message", {"to": f"u{i}"}, '{"error": "no route"}', failed=True)
    assert last.should_halt and last.code == "same_tool_failure_halt"


def test_browser_retry_after_action_is_not_a_replay():
    c = _HARD()
    nav = {"url": "https://example.test/app"}
    for _ in range(8):
        assert c.before_call("browser_navigate", nav).allows_execution
        c.after_call("browser_navigate", nav, '{"error": "timeout"}', failed=True)
        c.after_call("browser_click", {"selector": "#retry"}, '{"ok": true}', failed=False)
    assert c.halt_decision is None


def test_supervised_task_platforms_keep_warning_only_default():
    for platform in ("subagent", "api_server", "cli"):
        cfg = ToolCallGuardrailConfig.from_mapping({}, platform=platform)
        assert cfg.hard_stop_enabled is False, platform
    for platform in ("telegram", "discord", "cron", "kanban"):
        cfg = ToolCallGuardrailConfig.from_mapping({}, platform=platform)
        assert cfg.hard_stop_enabled is True, platform
