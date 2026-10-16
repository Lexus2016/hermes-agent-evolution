"""Optional snapshot failures retain compaction and emit redacted diagnostics."""

import concurrent.futures
import logging
from pathlib import Path

import pytest

from evolution.lib import parallel_compaction as pc


@pytest.mark.parametrize("background", [False, True])
def test_snapshot_failure_keeps_compaction_and_redacts_diagnostic(tmp_path, monkeypatch, caplog, background):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("HERMES_HOME", str(home / ".hermes"))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    secret = "private-transcript-and-secret"
    messages = [{"role": "user", "content": secret}]
    summary = [{"role": "assistant", "content": "compacted"}]

    def fail(*args, **kwargs):
        raise PermissionError(f"{home}/private-snapshot: {secret}")

    monkeypatch.setattr(pc, "write_precompaction_snapshot", fail)
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        compactor = pc.ParallelCompactor(executor=executor if background else None)
        compactor._snapshot_path = home / "previous-snapshot"
        with caplog.at_level(logging.WARNING, logger=pc.__name__):
            assert compactor.start_async_compaction(messages, 123, lambda snapshot: summary)
        if background:
            compactor._future.result(timeout=5)
        assert compactor.poll_status() is pc.CompactionState.READY_TO_SWAP
        assert compactor.snapshot_path is None
        assert compactor.snapshot.messages == messages
        assert compactor.snapshot.messages is not messages
        assert compactor.last_error is None
        assert compactor.try_apply_swap(messages) == summary
        assert compactor.state is pc.CompactionState.SWAPPED
    assert "PermissionError" in caplog.text
    assert "continuing in memory" in caplog.text
    assert secret not in caplog.text
    assert str(home) not in caplog.text
    record = next(record for record in caplog.records if record.name == pc.__name__)
    assert record.exc_info[2] is None
    assert str(record.exc_info[1]) == "Snapshot failure details redacted"
