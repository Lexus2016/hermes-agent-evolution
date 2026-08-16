# -*- coding: utf-8 -*-
"""Tests for pre-compaction snapshot virtual file storage and recovery (Issue #2471)."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from agent.context_compressor import ContextCompressor
from evolution.lib.parallel_compaction import (
    CompactionSnapshot,
    ParallelCompactionConfig,
    ParallelCompactor,
    read_precompaction_snapshot,
    read_precompaction_snapshot_text,
    write_precompaction_snapshot,
)
from run_agent import AIAgent


@pytest.fixture
def sample_messages():
    return [
        {"role": "system", "content": "You are Hermes."},
        {"role": "user", "content": "Run tests and examine logs"},
        {
            "role": "assistant",
            "content": "Running command",
            "tool_calls": [
                {
                    "id": "call_1",
                    "function": {
                        "name": "terminal",
                        "arguments": '{"command": "pytest"}',
                    },
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call_1", "content": "All 10 tests passed"},
        {"role": "assistant", "content": "Tests passed successfully."},
    ]


def test_compaction_snapshot_to_from_dict(sample_messages):
    snap = CompactionSnapshot(
        trigger_index=len(sample_messages),
        message_count=len(sample_messages),
        messages=sample_messages,
        token_count_at_trigger=1500,
    )
    d = snap.to_dict()
    assert d["trigger_index"] == 5
    assert d["message_count"] == 5
    assert d["token_count_at_trigger"] == 1500
    assert len(d["messages"]) == 5
    assert "snapshot_id" in d
    assert "timestamp" in d

    restored = CompactionSnapshot.from_dict(d)
    assert restored.snapshot_id == snap.snapshot_id
    assert restored.trigger_index == snap.trigger_index
    assert restored.message_count == snap.message_count
    assert restored.token_count_at_trigger == snap.token_count_at_trigger
    assert restored.messages == snap.messages


def test_compaction_snapshot_to_readable_text(sample_messages):
    snap = CompactionSnapshot(
        trigger_index=len(sample_messages),
        message_count=len(sample_messages),
        messages=sample_messages,
        token_count_at_trigger=1500,
        snapshot_id="testsnap123",
    )
    text = snap.to_readable_text()
    assert "# Pre-Compaction Snapshot [testsnap123]" in text
    assert "### Turn 1 (system)" in text
    assert "You are Hermes." in text
    assert "### Turn 2 (user)" in text
    assert "Run tests and examine logs" in text
    assert "```tool_call:terminal" in text
    assert "All 10 tests passed" in text


def test_write_and_read_precompaction_snapshot(sample_messages, tmp_path):
    snap = CompactionSnapshot(
        trigger_index=len(sample_messages),
        message_count=len(sample_messages),
        messages=sample_messages,
        token_count_at_trigger=1500,
    )
    path = write_precompaction_snapshot(
        snap, snapshot_dir=tmp_path, session_id="session_abc"
    )
    assert path.exists()
    assert "session_abc" in path.name
    assert path.name.endswith(".json")

    loaded = read_precompaction_snapshot(path)
    assert loaded.snapshot_id == snap.snapshot_id
    assert loaded.messages == snap.messages
    assert loaded.trigger_index == snap.trigger_index

    text = read_precompaction_snapshot_text(path)
    assert f"Pre-Compaction Snapshot [{snap.snapshot_id}]" in text
    assert "All 10 tests passed" in text


def test_parallel_compactor_writes_snapshot_on_async_trigger(sample_messages, tmp_path):
    config = ParallelCompactionConfig(
        enabled=True,
        hard_token_limit=1000,
        headroom_ratio=0.2,
        snapshot_dir=str(tmp_path),
    )
    compactor = ParallelCompactor(config=config, session_id="sess_123")

    def mock_summarize(msgs):
        return [{"role": "system", "content": "Summary"}]

    ok = compactor.start_async_compaction(
        messages=sample_messages,
        current_tokens=900,
        summarize_fn=mock_summarize,
    )
    assert ok is True
    assert compactor.snapshot is not None
    assert compactor.snapshot_path is not None
    assert compactor.snapshot_path.exists()

    # Read back through compactor methods
    snap_read = compactor.read_snapshot()
    assert snap_read is not None
    assert snap_read.snapshot_id == compactor.snapshot.snapshot_id

    text_read = compactor.read_snapshot_text()
    assert text_read is not None
    assert "All 10 tests passed" in text_read


def test_parallel_compactor_save_snapshot_disabled(sample_messages, tmp_path):
    config = ParallelCompactionConfig(
        enabled=True,
        hard_token_limit=1000,
        headroom_ratio=0.2,
        save_snapshot=False,
        snapshot_dir=str(tmp_path),
    )
    compactor = ParallelCompactor(config=config)

    def mock_summarize(msgs):
        return [{"role": "system", "content": "Summary"}]

    ok = compactor.start_async_compaction(
        messages=sample_messages,
        current_tokens=900,
        summarize_fn=mock_summarize,
    )
    assert ok is True
    assert compactor.snapshot is not None
    assert compactor.snapshot_path is None


def test_context_compressor_snapshot_integration(sample_messages, tmp_path):
    compressor = ContextCompressor(model="test-model")
    pc = compressor.parallel_compactor
    assert pc is not None
    pc.config.snapshot_dir = str(tmp_path)
    compressor.bind_session_state(session_id="session_xyz")

    compressor.compress = MagicMock(
        return_value=[{"role": "system", "content": "Summary"}]
    )

    started = compressor.start_parallel_compaction(
        messages=sample_messages,
        current_tokens=90000,
    )
    assert started is True
    assert compressor.snapshot_path is not None
    assert compressor.snapshot_path.exists()

    recovered = compressor.read_precompaction_snapshot()
    assert recovered is not None
    assert len(recovered.messages) == len(sample_messages)

    recovered_text = compressor.read_precompaction_snapshot_text()
    assert "All 10 tests passed" in recovered_text


def test_ai_agent_precompaction_snapshot_access(sample_messages, tmp_path):
    agent = AIAgent(
        api_key="mock-key",
        base_url="http://localhost:8080/v1",
        model="test-model",
        quiet_mode=True,
    )
    if agent.context_compressor and agent.parallel_compactor:
        agent.parallel_compactor.config.snapshot_dir = str(tmp_path)
        agent.context_compressor.compress = MagicMock(
            return_value=[{"role": "system", "content": "Summary"}]
        )
        agent.context_compressor.start_parallel_compaction(
            messages=sample_messages,
            current_tokens=90000,
        )
        assert agent.precompaction_snapshot_path is not None
        assert agent.precompaction_snapshot_path.exists()

        snap = agent.read_precompaction_snapshot()
        assert snap is not None
        assert len(snap.messages) == len(sample_messages)

        text = agent.read_precompaction_snapshot_text()
        assert "All 10 tests passed" in text
