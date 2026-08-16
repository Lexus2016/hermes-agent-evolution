# -*- coding: utf-8 -*-
"""Tests for the pre-compaction snapshot (Issue #2471)."""

from __future__ import annotations

from evolution.lib.parallel_compaction import (
    ParallelCompactor,
    read_precompaction_snapshot,
    write_precompaction_snapshot,
)


def test_write_and_read_snapshot_roundtrip(tmp_path):
    messages = [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "world"},
    ]
    path = write_precompaction_snapshot(messages, snapshot_dir=str(tmp_path))
    assert path.startswith(str(tmp_path))

    recovered = read_precompaction_snapshot(path)
    assert recovered == messages


def test_compactor_writes_snapshot_on_compaction(tmp_path):
    compactor = ParallelCompactor(snapshot_dir=str(tmp_path))

    def summarizer(msgs):
        return [{"role": "system", "content": "summary"}]

    msgs = [{"role": "user", "content": "hi"}]
    assert compactor.start_async_compaction(msgs, 90000, summarizer)

    assert compactor.snapshot_path is not None
    assert compactor.snapshot_path.startswith(str(tmp_path))

    # The agent can re-read the dropped context later.
    recovered = compactor.read_snapshot()
    assert recovered == msgs


def test_read_snapshot_none_when_not_captured():
    compactor = ParallelCompactor()
    assert compactor.read_snapshot() is None


def test_reset_clears_snapshot_path(tmp_path):
    compactor = ParallelCompactor(snapshot_dir=str(tmp_path))

    def summarizer(msgs):
        return [{"role": "system", "content": "summary"}]

    compactor.start_async_compaction(
        [{"role": "user", "content": "x"}], 90000, summarizer
    )
    assert compactor.snapshot_path is not None

    compactor.reset()
    assert compactor.snapshot_path is None
    assert compactor.read_snapshot() is None
