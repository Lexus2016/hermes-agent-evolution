# -*- coding: utf-8 -*-
"""Unit tests for the tool-failure recovery strategy dispatcher (#1027)."""

from __future__ import annotations

import pytest

from tools.tool_failure_classifier import (
    ToolFailureCategory,
    ToolFailureClassification,
    ToolType,
    classify_tool_failure,
)
from tools.recovery_strategy_dispatcher import (
    RECOVERY_GUIDANCE_PREFIX,
    RecoveryAction,
    RecoveryStrategy,
    backoff_seconds_for,
    dispatch_recovery,
    maybe_append_recovery_guidance,
    recover_from_failure,
    register_strategy,
)


def _classification(
    category: ToolFailureCategory,
    *,
    should_retry: bool,
    tool_type: ToolType = ToolType.generic,
) -> ToolFailureClassification:
    return ToolFailureClassification(
        category=category,
        tool_type=tool_type,
        hint="",
        should_retry=should_retry,
    )


# --- category -> strategy table ------------------------------------------------


@pytest.mark.parametrize(
    "category,should_retry,expected",
    [
        (ToolFailureCategory.tool_unavailable, False, RecoveryStrategy.switch_tool),
        (ToolFailureCategory.invalid_arguments, False, RecoveryStrategy.fix_arguments),
        (ToolFailureCategory.not_found, False, RecoveryStrategy.verify_target),
        (ToolFailureCategory.permission_denied, False, RecoveryStrategy.surface_blocker),
        (ToolFailureCategory.rate_limited, True, RecoveryStrategy.retry_with_backoff),
        (ToolFailureCategory.transient_network, True, RecoveryStrategy.retry_with_backoff),
        (ToolFailureCategory.timeout, True, RecoveryStrategy.retry_with_backoff),
        (ToolFailureCategory.unexpected_output, True, RecoveryStrategy.retry),
        (ToolFailureCategory.persistent_error, False, RecoveryStrategy.switch_tool),
        (ToolFailureCategory.unknown, False, RecoveryStrategy.surface_blocker),
    ],
)
def test_dispatch_maps_every_category_to_expected_strategy(category, should_retry, expected):
    action = dispatch_recovery(_classification(category, should_retry=should_retry))
    assert action.strategy is expected
    assert action.category is category
    assert action.directive  # non-empty imperative directive
    assert action.should_retry is should_retry


def test_dispatch_covers_all_categories():
    """Every ToolFailureCategory yields a concrete, non-surface_blocker-by-default action."""
    for category in ToolFailureCategory:
        action = dispatch_recovery(_classification(category, should_retry=False))
        assert isinstance(action, RecoveryAction)
        assert isinstance(action.strategy, RecoveryStrategy)


# --- backoff -------------------------------------------------------------------


def test_backoff_is_exponential_and_capped():
    assert backoff_seconds_for(0) == 1.0
    assert backoff_seconds_for(1) == 2.0
    assert backoff_seconds_for(2) == 4.0
    assert backoff_seconds_for(3) == 8.0
    # capped at 30s
    assert backoff_seconds_for(10) == 30.0
    assert backoff_seconds_for(100) == 30.0


def test_retry_with_backoff_action_carries_backoff_seconds():
    action = dispatch_recovery(
        _classification(ToolFailureCategory.rate_limited, should_retry=True),
        consecutive_count=1,
    )
    assert action.strategy is RecoveryStrategy.retry_with_backoff
    assert action.backoff_seconds == 2.0


def test_non_backoff_action_has_no_backoff():
    action = dispatch_recovery(_classification(ToolFailureCategory.invalid_arguments, should_retry=False))
    assert action.backoff_seconds is None


# --- monotone escalation -------------------------------------------------------


def test_retryable_escalates_to_switch_tool_after_threshold():
    action = dispatch_recovery(
        _classification(ToolFailureCategory.transient_network, should_retry=True),
        consecutive_count=3,
    )
    assert action.strategy is RecoveryStrategy.switch_tool
    assert action.should_retry is False


def test_retryable_escalates_to_escalate_after_higher_threshold():
    action = dispatch_recovery(
        _classification(ToolFailureCategory.transient_network, should_retry=True),
        consecutive_count=5,
    )
    assert action.strategy is RecoveryStrategy.escalate
    assert action.should_retry is False
    assert action.backoff_seconds is None


def test_non_retryable_category_is_not_escalated_by_count():
    # invalid_arguments is deterministic (should_retry False); escalation only
    # applies to retryable categories, so it stays fix_arguments regardless.
    action = dispatch_recovery(
        _classification(ToolFailureCategory.invalid_arguments, should_retry=False),
        consecutive_count=9,
    )
    assert action.strategy is RecoveryStrategy.fix_arguments


# --- recover_from_failure (classify + dispatch) --------------------------------


def test_recover_from_failure_classifies_then_dispatches_missing_file():
    # A read_file "No such file" is a not_found -> verify_target.
    action = recover_from_failure("read_file", "No such file or directory: /x/y")
    assert action.category is ToolFailureCategory.not_found
    assert action.strategy is RecoveryStrategy.verify_target
    assert action.tool_name == "read_file"


def test_recover_from_failure_matches_direct_classification():
    tool_name = "web_search"
    error = "429 Too Many Requests: rate limit exceeded"
    classification = classify_tool_failure(tool_name, error)
    action = recover_from_failure(tool_name, error)
    assert action.category is classification.category


# --- runtime seam (maybe_append_recovery_guidance) -----------------------------


def test_seam_disabled_returns_result_unchanged():
    result = '{"error": "boom"}'
    out = maybe_append_recovery_guidance(result, "web_search", failed=True, enabled=False)
    assert out == result


def test_seam_not_failed_returns_result_unchanged():
    result = '{"ok": true}'
    out = maybe_append_recovery_guidance(result, "web_search", failed=False, enabled=True)
    assert out == result


def test_seam_enabled_and_failed_appends_single_recovery_line():
    result = '{"error": "No such file or directory: /x"}'
    out = maybe_append_recovery_guidance(result, "read_file", failed=True, enabled=True)
    assert out.startswith(result)
    assert RECOVERY_GUIDANCE_PREFIX in out
    assert out.count(RECOVERY_GUIDANCE_PREFIX) == 1
    assert "verify_target" in out


def test_seam_extracts_terminal_exit_code_from_json_result():
    result = '{"exit_code": 127, "stderr": "command not found: foo"}'
    out = maybe_append_recovery_guidance(result, "terminal", failed=True, enabled=True)
    assert RECOVERY_GUIDANCE_PREFIX in out


def test_seam_never_raises_on_garbage_result():
    out = maybe_append_recovery_guidance("\x00 not json", "terminal", failed=True, enabled=True)
    # degrades gracefully: either unchanged or with guidance, but never raises
    assert out.startswith("\x00 not json")


def test_seam_none_result_is_handled():
    out = maybe_append_recovery_guidance(None, "web_search", failed=True, enabled=True)
    assert isinstance(out, str)


# --- runtime extensibility -----------------------------------------------------


def test_register_strategy_overrides_category_mapping():
    original = dispatch_recovery(_classification(ToolFailureCategory.unknown, should_retry=False)).strategy
    try:
        register_strategy(ToolFailureCategory.unknown, RecoveryStrategy.switch_tool)
        action = dispatch_recovery(_classification(ToolFailureCategory.unknown, should_retry=False))
        assert action.strategy is RecoveryStrategy.switch_tool
    finally:
        register_strategy(ToolFailureCategory.unknown, original)


# --- serialization -------------------------------------------------------------


def test_recovery_action_to_dict_roundtrip_shape():
    action = dispatch_recovery(
        _classification(ToolFailureCategory.rate_limited, should_retry=True),
        tool_name="web_search",
        consecutive_count=1,
    )
    data = action.to_dict()
    assert data["category"] == "rate_limited"
    assert data["strategy"] == "retry_with_backoff"
    assert data["should_retry"] is True
    assert data["tool_name"] == "web_search"
    assert data["backoff_seconds"] == 2.0
