"""Tests for pluggable per-policy tool-call interceptors."""

from agent.policy_interceptors import (
    PolicyInterceptorRegistry,
    PolicyOutcome,
    RegisteredPolicy,
    ToolCallContext,
    build_registry_from_config,
    make_deny_tools,
    make_require_read_before_write,
)
from agent.tool_guardrails import (
    ToolCallGuardrailConfig,
    ToolCallGuardrailController,
    ToolCallSignature,
)


def _registry(*policies: RegisteredPolicy) -> PolicyInterceptorRegistry:
    return PolicyInterceptorRegistry(list(policies))


# ── PolicyOutcome / context primitives ──────────────────────────────────────


def test_policy_outcome_constructors_set_expected_actions():
    assert PolicyOutcome.allow().action == "allow"
    deny = PolicyOutcome.deny("nope")
    assert deny.action == "deny"
    assert deny.message == "nope"
    rewrite = PolicyOutcome.rewrite({"a": 1}, "tidied")
    assert rewrite.action == "rewrite"
    assert rewrite.rewritten_args == {"a": 1}
    assert rewrite.message == "tidied"


def test_empty_registry_is_inert_and_allows_everything():
    registry = PolicyInterceptorRegistry()
    assert registry.enabled is False
    decision = registry.evaluate("write_file", {"path": "/tmp/x"})
    assert decision.allows_execution is True
    assert decision.action == "allow"


# ── deny_tools policy ────────────────────────────────────────────────────────


def test_deny_tools_blocks_named_tool_and_allows_others():
    registry = _registry(
        RegisteredPolicy("no-process", make_deny_tools(frozenset({"process"})))
    )
    assert registry.enabled is True

    blocked = registry.evaluate("process", {"action": "spawn"})
    assert blocked.allows_execution is False
    assert blocked.action == "block"
    assert blocked.code == "policy_deny:no-process"
    assert "disabled by policy" in blocked.message

    allowed = registry.evaluate("read_file", {"path": "/tmp/x"})
    assert allowed.allows_execution is True


# ── require_read_before_write policy ─────────────────────────────────────────


def _read_before_write_registry() -> PolicyInterceptorRegistry:
    policy = make_require_read_before_write(
        write_tools=frozenset({"write_file", "patch"}),
        read_tools=frozenset({"read_file"}),
        path_keys=("path", "file_path"),
    )
    return _registry(RegisteredPolicy("read-before-write", policy))


def test_write_before_read_is_denied_with_recoverable_message():
    registry = _read_before_write_registry()

    decision = registry.evaluate("write_file", {"path": "/repo/a.py", "content": "x"})
    assert decision.allows_execution is False
    assert decision.action == "block"
    assert decision.code == "policy_deny:read-before-write"
    assert "/repo/a.py" in decision.message
    assert "Read the file first" in decision.message


def test_write_after_successful_read_of_same_path_is_allowed():
    registry = _read_before_write_registry()

    registry.record_observation("read_file", {"path": "/repo/a.py"}, failed=False)
    decision = registry.evaluate("write_file", {"path": "/repo/a.py", "content": "x"})
    assert decision.allows_execution is True
    assert decision.action == "allow"


def test_write_after_failed_read_is_still_denied():
    registry = _read_before_write_registry()

    registry.record_observation("read_file", {"path": "/repo/a.py"}, failed=True)
    decision = registry.evaluate("write_file", {"path": "/repo/a.py", "content": "x"})
    assert decision.allows_execution is False


def test_read_of_a_different_path_does_not_unlock_the_write():
    registry = _read_before_write_registry()

    registry.record_observation("read_file", {"path": "/repo/other.py"}, failed=False)
    decision = registry.evaluate("write_file", {"path": "/repo/a.py", "content": "x"})
    assert decision.allows_execution is False


def test_write_with_no_determinable_path_fails_open():
    registry = _read_before_write_registry()

    decision = registry.evaluate("write_file", {"content": "x"})
    assert decision.allows_execution is True


def test_reset_for_turn_clears_observation_ledger():
    registry = _read_before_write_registry()

    registry.record_observation("read_file", {"path": "/repo/a.py"}, failed=False)
    registry.reset_for_turn()
    decision = registry.evaluate("write_file", {"path": "/repo/a.py", "content": "x"})
    assert decision.allows_execution is False


# ── rewrite composition ──────────────────────────────────────────────────────


def test_rewrite_outcome_is_composed_in_order_for_later_policies():
    def add_flag(ctx: ToolCallContext) -> PolicyOutcome:
        args = dict(ctx.args)
        args["flag"] = True
        return PolicyOutcome.rewrite(args)

    def deny_when_flagged(ctx: ToolCallContext) -> PolicyOutcome:
        if ctx.args.get("flag") is True:
            return PolicyOutcome.deny("flag present")
        return PolicyOutcome.allow()

    registry = _registry(
        RegisteredPolicy("add-flag", add_flag),
        RegisteredPolicy("deny-flagged", deny_when_flagged),
    )
    decision = registry.evaluate("write_file", {"path": "/tmp/x"})
    assert decision.allows_execution is False
    assert decision.code == "policy_deny:deny-flagged"


def test_first_matching_deny_wins():
    registry = _registry(
        RegisteredPolicy("a", make_deny_tools(frozenset({"process"}))),
        RegisteredPolicy("b", make_deny_tools(frozenset({"process"}))),
    )
    decision = registry.evaluate("process", {})
    assert decision.code == "policy_deny:a"


# ── config-driven registry construction ─────────────────────────────────────


def test_build_registry_disabled_by_default():
    assert build_registry_from_config(None).enabled is False
    assert build_registry_from_config({}).enabled is False
    assert build_registry_from_config({"enabled": False, "policies": []}).enabled is False


def test_build_registry_enables_named_builtin_policies_in_order():
    registry = build_registry_from_config(
        {
            "enabled": True,
            "policies": [
                {"name": "rbw", "policy": "require_read_before_write"},
                {"policy": "deny_tools", "options": {"tools": ["process"]}},
            ],
        }
    )
    assert registry.enabled is True
    assert registry.policy_names == ("rbw", "deny_tools")

    # deny_tools is active
    assert registry.evaluate("process", {}).allows_execution is False
    # require_read_before_write is active
    assert registry.evaluate("write_file", {"path": "/x"}).allows_execution is False


def test_build_registry_skips_unknown_and_malformed_entries():
    registry = build_registry_from_config(
        {
            "enabled": True,
            "policies": [
                "not-a-mapping",
                {"name": "x"},  # missing policy id
                {"policy": "does_not_exist"},
                {"policy": "deny_tools", "options": {"tools": ["process"]}},
            ],
        }
    )
    assert registry.policy_names == ("deny_tools",)


def test_build_registry_accepts_string_truthy_enabled_flag():
    registry = build_registry_from_config(
        {"enabled": "yes", "policies": [{"policy": "deny_tools", "options": {"tools": ["x"]}}]}
    )
    assert registry.enabled is True


# ── controller integration (no new dispatch wiring) ──────────────────────────


def test_controller_blocks_via_policy_before_loop_checks_even_with_hard_stop_disabled():
    registry = build_registry_from_config(
        {"enabled": True, "policies": [{"policy": "require_read_before_write"}]}
    )
    controller = ToolCallGuardrailController(
        ToolCallGuardrailConfig(hard_stop_enabled=False),
        policy_registry=registry,
    )

    decision = controller.before_call("write_file", {"path": "/repo/a.py", "content": "x"})
    assert decision.allows_execution is False
    assert decision.action == "block"
    assert decision.code == "policy_deny:require_read_before_write"
    assert controller.halt_decision is decision


def test_controller_records_reads_via_after_call_then_allows_the_write():
    registry = build_registry_from_config(
        {"enabled": True, "policies": [{"policy": "require_read_before_write"}]}
    )
    controller = ToolCallGuardrailController(
        ToolCallGuardrailConfig(hard_stop_enabled=False),
        policy_registry=registry,
    )

    # Successful read recorded through the normal after_call observation path.
    assert controller.before_call("read_file", {"path": "/repo/a.py"}).allows_execution
    controller.after_call("read_file", {"path": "/repo/a.py"}, "file contents", failed=False)

    decision = controller.before_call("write_file", {"path": "/repo/a.py", "content": "x"})
    assert decision.allows_execution is True


def test_controller_reset_for_turn_clears_policy_ledger():
    registry = build_registry_from_config(
        {"enabled": True, "policies": [{"policy": "require_read_before_write"}]}
    )
    controller = ToolCallGuardrailController(
        ToolCallGuardrailConfig(hard_stop_enabled=False),
        policy_registry=registry,
    )

    controller.after_call("read_file", {"path": "/repo/a.py"}, "contents", failed=False)
    controller.reset_for_turn()
    decision = controller.before_call("write_file", {"path": "/repo/a.py", "content": "x"})
    assert decision.allows_execution is False


def test_policy_decision_metadata_never_exposes_raw_args():
    registry = build_registry_from_config(
        {"enabled": True, "policies": [{"policy": "deny_tools", "options": {"tools": ["process"]}}]}
    )
    decision = registry.evaluate("process", {"secret": "token-value"})
    metadata = decision.to_metadata()
    assert "token-value" not in str(metadata)
    assert isinstance(decision.signature, ToolCallSignature)
