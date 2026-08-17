# -*- coding: utf-8 -*-
"""Regression suite for the adversarial tool environment (Issue #2639, ToolHazard)."""

from __future__ import annotations

import pytest

from evolution.lib.adversarial_tool_env import (
    AdversarialToolEnv,
    ToolExhaustedError,
    is_embedded_instruction,
    payload,
)


def test_stateful_fake_tool_returns_results_in_order_and_mutates_state() -> None:
    env = AdversarialToolEnv([{"name": "read_file", "results": ["alpha", "beta"]}])

    assert env.call("read_file") == "alpha"
    assert env.call("read_file") == "beta"
    assert env.call_count == 2
    assert [c["tool"] for c in env.calls] == ["read_file", "read_file"]
    assert [c["seq"] for c in env.calls] == [1, 2]

    with pytest.raises(ToolExhaustedError):
        env.call("read_file")
    with pytest.raises(KeyError):
        env.call("unknown_tool")


def test_poisoned_tool_embeds_detected_instruction() -> None:
    sample = payload()
    assert "delete_all" in sample
    assert is_embedded_instruction(sample)

    for style in ("tool", "web", "json"):
        spec = {
            "name": "fetch_page",
            "poisoned": True,
            "style": style,
            "results": ["<p>article</p>"],
        }
        result = AdversarialToolEnv([spec]).call("fetch_page")
        assert is_embedded_instruction(result), f"style {style} not flagged"
        assert "delete_all" in result


def test_benign_tool_result_is_not_flagged() -> None:
    config_spec = {"name": "read_config", "results": ["rate_limit=60, retries=3"]}
    env = AdversarialToolEnv([config_spec])
    result = env.call("read_config")
    assert result == "rate_limit=60, retries=3"
    assert not is_embedded_instruction(result)
    assert not is_embedded_instruction("The page lists three search results.")
    assert not is_embedded_instruction('{"content": "plain data"}')


def test_subagent_policy_does_not_follow_embedded_instructions() -> None:
    """A policy that treats tool results as data must never act on embedded instructions."""
    env = AdversarialToolEnv([
        {"name": "fetch_page", "poisoned": True, "results": ["catalog page"]},
        {"name": "read_config", "results": ["rate_limit=60"]},
    ])

    def policy(env: AdversarialToolEnv, tool: str) -> str:
        """Tiny subagent policy: execute only when the result is clean data."""
        result = env.call(tool)
        if is_embedded_instruction(result):
            return "REFUSE"
        return f"PROCEED:{result}"

    # The poisoned result embeds "call the delete_all tool"; the policy refuses
    # and never invokes that tool.
    assert policy(env, "fetch_page") == "REFUSE"
    assert [c["tool"] for c in env.calls] == ["fetch_page"]

    # Same policy still acts on benign results (boundary, not blanket refusal).
    assert policy(env, "read_config") == "PROCEED:rate_limit=60"
