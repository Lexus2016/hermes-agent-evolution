"""Tests for the conflict-snapshot sidecar — issue #2424.

Restored 2026-08-15 (implementation stage): the original test file from the
2026-08-14-late run was lost when the working tree was reset by the
00:42Z upstream-sync dispatch. Targets `snapshot_conflict` +
the Case-1 warning augmentation in tools/file_state.py (see patch
evolution/implementation/patches-2026-08-15/2424-conflict-snapshot-sidecar-v2-restage.patch).

NOTE: collection succeeds on an UNPATCHED tree (all imported names exist),
but every TestSnapshotConflict test and the "merge both versions" /
sidecar-existence assertions in TestCheckStaleWarnsAndPreserves require the
v2-restage patch applied — they fail with AttributeError
(`file_state.snapshot_conflict`) until then. The stage report and handoff
note cf41a0f068614b38 sequence the patch-before-test step.
"""

import sys
import time

import pytest

from tools import file_state
from tools.file_state import (
    check_stale,
    get_registry,
    note_write,
    record_read,
)


@pytest.fixture(autouse=True)
def _clean_registry(monkeypatch):
    monkeypatch.delenv("HERMES_DISABLE_FILE_STATE_GUARD", raising=False)
    get_registry().clear()
    yield
    get_registry().clear()


def _write(path, content):
    path.write_text(content, encoding="utf-8")


def _sidecars(path):
    return sorted(
        p
        for p in path.parent.iterdir()
        if p.name.startswith(f"{path.name}.conflict-")
    )


class TestSnapshotConflict:
    def test_preserves_sibling_bytes(self, tmp_path):
        target = tmp_path / "report.md"
        _write(target, "sibling content")
        sidecar = file_state.snapshot_conflict(str(target), "writer-A")
        assert sidecar is not None
        from pathlib import Path

        assert Path(sidecar).read_text(encoding="utf-8") == "sibling content"
        # original untouched — the CALLER performs the overwrite
        assert target.read_text(encoding="utf-8") == "sibling content"

    def test_sidecar_name_carries_writer_and_ts(self, tmp_path):
        target = tmp_path / "report.md"
        _write(target, "x")
        sidecar = file_state.snapshot_conflict(str(target), "sa-9")
        assert ".conflict-sa-9-" in sidecar

    def test_missing_file_returns_none(self, tmp_path):
        assert file_state.snapshot_conflict(str(tmp_path / "nope.md"), "w") is None

    def test_directory_target_returns_none(self, tmp_path):
        # A directory in place of the file: is_file() is False → None,
        # no exception. (OSError paths are also swallowed.)
        assert file_state.snapshot_conflict(str(tmp_path), "w") is None

    def test_respects_disable_env(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_DISABLE_FILE_STATE_GUARD", "1")
        target = tmp_path / "f.md"
        _write(target, "x")
        assert file_state.snapshot_conflict(str(target), "w") is None

    def test_cap_five_per_path(self, tmp_path):
        target = tmp_path / "hot.md"
        _write(target, "v0")
        made = [file_state.snapshot_conflict(str(target), "writer-A") for _ in range(8)]
        assert all(made)
        remaining = _sidecars(target)
        assert len(remaining) <= 5


class TestCheckStaleWarnsAndPreserves:
    def test_case1_after_read_variant_names_sidecar(self, tmp_path):
        # Sibling B writes AFTER agent A's read → Case 1, second variant.
        # The sleep guarantees writer_ts > read_ts on coarse clocks.
        target = tmp_path / "canon.md"
        _write(target, "v1")
        record_read("A", str(target))
        time.sleep(0.02)
        note_write("B", str(target))
        msg = check_stale("A", str(target))
        assert msg is not None
        assert "sibling subagent" in msg
        assert "Re-read the file before writing" in msg
        assert "merge both versions" in msg  # post-patch: sidecar surfaced
        assert _sidecars(target), "sidecar must exist after Case-1 warning"

    def test_case1_never_read_variant_warns(self, tmp_path):
        # Agent A NEVER read the file; sibling B wrote it → Case 1, first
        # variant. record_read is deliberately absent for A.
        target = tmp_path / "canon.md"
        _write(target, "sibling bytes")
        note_write("B", str(target))
        time.sleep(0.02)
        msg = check_stale("A", str(target))
        assert msg is not None
        assert "never read it" in msg
        assert "Read the file before writing" in msg

    def test_fresh_write_no_warning_no_sidecar(self, tmp_path):
        target = tmp_path / "ok.md"
        _write(target, "v1")
        record_read("A", str(target))
        time.sleep(0.02)
        note_write("A", str(target))
        assert check_stale("A", str(target)) is None
        assert not _sidecars(target)

    def test_disabled_guard_no_warning_no_sidecar(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_DISABLE_FILE_STATE_GUARD", "1")
        target = tmp_path / "g.md"
        _write(target, "v1")
        record_read("A", str(target))
        time.sleep(0.02)
        note_write("B", str(target))
        assert check_stale("A", str(target)) is None
        assert not _sidecars(target)


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
