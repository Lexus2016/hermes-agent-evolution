# -*- coding: utf-8 -*-
"""Unit tests for early-trigger parallel compaction (Issue #2469)."""

from __future__ import annotations

import concurrent.futures
import time
from typing import Any, Dict, List

import pytest

from evolution.lib.parallel_compaction import (
    CompactionSnapshot,
    CompactionState,
    ParallelCompactionConfig,
    ParallelCompactor,
)


def test_config_headroom_and_early_trigger():
    config = ParallelCompactionConfig(
        hard_token_limit=100000,
        headroom_ratio=0.15,
    )
    assert config.headroom_tokens == 15000
    assert config.early_trigger_threshold == 85000

    config_explicit = ParallelCompactionConfig(
        hard_token_limit=100000,
        explicit_headroom_tokens=20000,
    )
    assert config_explicit.headroom_tokens == 20000
    assert config_explicit.early_trigger_threshold == 80000

    d = config.to_dict()
    restored = ParallelCompactionConfig.from_dict(d)
    assert restored.hard_token_limit == 100000


def test_should_trigger_evaluation():
    config = ParallelCompactionConfig(hard_token_limit=10000, headroom_ratio=0.2)
    compactor = ParallelCompactor(config=config)

    # 10000 * 0.8 = 8000
    assert not compactor.should_trigger(7999)
    assert compactor.should_trigger(8000)
    assert compactor.should_trigger(9500)

    config_disabled = ParallelCompactionConfig(enabled=False)
    compactor_disabled = ParallelCompactor(config=config_disabled)
    assert not compactor_disabled.should_trigger(90000)


def test_async_compaction_and_safe_swap():
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=2)
    compactor = ParallelCompactor(executor=executor)

    def slow_summarizer(msgs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        time.sleep(0.05)
        return [{"role": "system", "content": "Summary of prior turns"}]

    initial_messages = [
        {"role": "user", "content": "Hello step 1"},
        {"role": "assistant", "content": "Response step 1"},
    ]

    # Start background compaction
    assert compactor.start_async_compaction(
        initial_messages, current_tokens=86000, summarize_fn=slow_summarizer
    )
    assert compactor.state == CompactionState.COMPACTING

    # Agent executes more turns in parallel
    live_messages = list(initial_messages)
    live_messages.append({"role": "user", "content": "Step 2 in parallel"})
    live_messages.append({
        "role": "assistant",
        "content": "Response step 2 in parallel",
    })

    # Wait for future to finish
    time.sleep(0.1)
    status = compactor.poll_status()
    assert status == CompactionState.READY_TO_SWAP

    # Apply swap at safe boundary
    swapped = compactor.try_apply_swap(live_messages)
    assert swapped is not None
    assert compactor.state == CompactionState.SWAPPED
    assert compactor.swap_count == 1

    # Should contain summarized prefix + delta messages (step 2)
    assert len(swapped) == 3
    assert swapped[0] == {"role": "system", "content": "Summary of prior turns"}
    assert swapped[1] == {"role": "user", "content": "Step 2 in parallel"}
    assert swapped[2] == {"role": "assistant", "content": "Response step 2 in parallel"}

    executor.shutdown(wait=True)


def test_unsafe_swap_boundary_rejected():
    compactor = ParallelCompactor()

    def dummy_summarizer(msgs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        return [{"role": "system", "content": "summary"}]

    msgs = [{"role": "user", "content": "hi"}]
    compactor.start_async_compaction(msgs, 90000, dummy_summarizer)
    assert compactor.state == CompactionState.READY_TO_SWAP

    # Unsafe boundary: assistant with pending tool call
    unsafe_messages = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": None, "tool_calls": [{"name": "read_file"}]},
    ]
    assert not compactor.is_safe_swap_boundary(unsafe_messages)
    assert compactor.try_apply_swap(unsafe_messages) is None

    # Unsafe boundary: tool response without final assistant turn
    unsafe_tool_msg = [
        {"role": "tool", "content": "file contents"},
    ]
    assert not compactor.is_safe_swap_boundary(unsafe_tool_msg)
    assert compactor.try_apply_swap(unsafe_tool_msg) is None


def test_failed_compaction_handling():
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    compactor = ParallelCompactor(executor=executor)

    def failing_summarizer(msgs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        raise RuntimeError("Summarization LLM quota exceeded")

    msgs = [{"role": "user", "content": "test"}]
    compactor.start_async_compaction(msgs, 90000, failing_summarizer)

    time.sleep(0.05)
    assert compactor.poll_status() == CompactionState.FAILED
    assert compactor.last_error is not None
    assert "quota exceeded" in compactor.last_error

    compactor.reset()
    assert compactor.state == CompactionState.IDLE
    assert compactor.last_error is None

    executor.shutdown(wait=True)
