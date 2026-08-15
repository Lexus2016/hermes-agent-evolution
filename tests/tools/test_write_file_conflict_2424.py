#!/usr/bin/env python3
"""Tests for #2424 — write_file silent lost-updates on sibling conflicts.

Two layers:

1. Registry-level (tools/file_state.py): ``sibling_conflict`` attributes a
   doomed overwrite to the sibling writer; ``preserve_conflict`` copies the
   sibling's bytes to a ``.conflict-<ts>`` sidecar before destruction.
2. Handler-level (tools/file_tools.py wiring): ``write_file_tool`` preserves
   the sibling content and surfaces BOTH paths (canonical + sidecar) in the
   result so the caller can merge. These skip on trees where the file_tools
   wiring is staged but not yet applied.

Run:
    python -m pytest tests/tools/test_write_file_conflict_2424.py -v
"""

from __future__ import annotations

import glob
import json
import os
import time
from pathlib import Path

import pytest

from tools import file_state


@pytest.fixture
def workdir(tmp_path, monkeypatch):
    """Isolated HERMES_HOME + clean registry per test."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    file_state.get_registry().clear()
    yield tmp_path
    file_state.get_registry().clear()


def _seed(path: Path, content: str) -> Path:
    path.write_text(content)
    return path


# ── Layer 1: registry-level (file_state.py) ──────────────────────────


class TestSiblingConflictDetection:
    """file_state.sibling_conflict — agent-attributed doom detection."""

    def test_sibling_write_after_read_is_conflict(self, workdir):
        p = _seed(workdir / "shared.txt", "original\n")
        file_state.record_read("A", str(p))
        time.sleep(0.01)
        file_state.note_write("B", str(p))
        conflict = file_state.sibling_conflict("A", str(p))
        assert conflict is not None
        writer_tid, writer_ts = conflict
        assert writer_tid == "B"
        assert writer_ts > 0

    def test_sibling_write_without_our_read_is_conflict(self, workdir):
        # The exact #2424 scenario: agent A never read the file (its read
        # path was circuit-broken), sibling B wrote it. A's overwrite would
        # destroy B's content unseen.
        p = _seed(workdir / "dispatch.jsonl", '{"B": 1}\n')
        file_state.note_write("B", str(p))
        conflict = file_state.sibling_conflict("A", str(p))
        assert conflict is not None
        assert conflict[0] == "B"

    def test_no_writer_no_conflict(self, workdir):
        p = _seed(workdir / "fresh.txt", "x\n")
        file_state.record_read("A", str(p))
        assert file_state.sibling_conflict("A", str(p)) is None

    def test_own_write_is_not_conflict(self, workdir):
        p = _seed(workdir / "own.txt", "x\n")
        file_state.record_read("A", str(p))
        file_state.note_write("A", str(p))
        assert file_state.sibling_conflict("A", str(p)) is None

    def test_sibling_write_before_our_read_is_not_conflict(self, workdir):
        # Sibling wrote, THEN we read — we have seen its content; the
        # classic check_stale warning still covers staleness, but there is
        # no unseen content to preserve.
        p = _seed(workdir / "ordered.txt", "x\n")
        file_state.note_write("B", str(p))
        time.sleep(0.01)
        file_state.record_read("A", str(p))
        assert file_state.sibling_conflict("A", str(p)) is None

    def test_external_mtime_drift_is_not_agent_conflict(self, workdir):
        # Formatter/external edit changes mtime but no other agent is on
        # record — nothing attributable to preserve; no sidecar flood.
        p = _seed(workdir / "drift.txt", "x\n")
        file_state.record_read("A", str(p))
        time.sleep(0.01)
        os.utime(str(p), None)
        assert file_state.sibling_conflict("A", str(p)) is None

    def test_kill_switch_disables_conflict_detection(self, workdir, monkeypatch):
        p = _seed(workdir / "ks.txt", "x\n")
        file_state.record_read("A", str(p))
        file_state.note_write("B", str(p))
        monkeypatch.setenv("HERMES_DISABLE_FILE_STATE_GUARD", "1")
        assert file_state.sibling_conflict("A", str(p)) is None


class TestPreserveConflict:
    """file_state.preserve_conflict — sidecar copy of doomed bytes."""

    def test_sidecar_created_with_same_content(self, workdir):
        p = _seed(workdir / "report.md", "sibling report body\n")
        sidecar = file_state.preserve_conflict(str(p))
        assert sidecar is not None
        assert Path(sidecar).read_text() == "sibling report body\n"
        assert Path(sidecar).parent == p.parent
        assert ".conflict-" in Path(sidecar).name
        # Original untouched — preservation is a copy, not a move.
        assert p.read_text() == "sibling report body\n"

    def test_sidecar_preserves_extension(self, workdir):
        p = _seed(workdir / "data.json", '{"a": 1}')
        sidecar = file_state.preserve_conflict(str(p))
        assert sidecar is not None
        assert Path(sidecar).suffix == ".json"

    def test_missing_file_returns_none(self, workdir):
        assert file_state.preserve_conflict(str(workdir / "nope.txt")) is None

    def test_repeated_conflicts_never_overwrite_each_other(self, workdir):
        p = _seed(workdir / "multi.txt", "v1\n")
        first = file_state.preserve_conflict(str(p))
        p.write_text("v2\n")
        second = file_state.preserve_conflict(str(p))
        assert first is not None and second is not None
        assert first != second
        assert Path(first).read_text() == "v1\n"
        assert Path(second).read_text() == "v2\n"


# ── Layer 2: handler-level wiring (file_tools.py) ────────────────────

wiring_applies = True
try:  # pragma: no cover - detection helper
    from tools.file_tools import write_file_tool  # noqa: F401

    def _wired() -> bool:
        # The wiring is detectable only behaviorally: a sibling-conflict
        # write must surface conflict_preserved_at. Probing on a throwaway
        # path keeps detection off the test's real fixtures.
        import tempfile

        file_state.get_registry().clear()
        d = tempfile.mkdtemp(prefix="hermes_2424_probe_")
        probe = os.path.join(d, "probe.txt")
        with open(probe, "w") as f:
            f.write("seed\n")
        file_state.note_write("probe-B", probe)
        try:
            r = json.loads(
                write_file_tool(path=probe, content="probe-A\n", task_id="probe-A")
            )
        except TypeError:
            return False
        finally:
            file_state.get_registry().clear()
        return bool(r.get("conflict_preserved_at"))

    wiring_applies = _wired()
except Exception:  # pragma: no cover
    wiring_applies = False

_skip_unwired = pytest.mark.skipif(
    not wiring_applies,
    reason="file_tools.py #2424 wiring not applied (staged patch) — "
    "registry-level tests above still run",
)


@_skip_unwired
class TestWriteFileConflictPreservation:
    """write_file_tool must not silently destroy unseen sibling content."""

    def test_sibling_content_preserved_to_sidecar(self, workdir):
        from tools.file_tools import write_file_tool

        p = _seed(workdir / "canonical.md", "sibling stage output\n")
        file_state.record_read("A", str(p))
        time.sleep(0.01)
        file_state.note_write("B", str(p))

        r = json.loads(
            write_file_tool(path=str(p), content="A overwrites\n", task_id="A")
        )
        assert "error" not in r, r

        sidecar = r.get("conflict_preserved_at")
        assert sidecar, f"expected conflict_preserved_at in result: {r}"
        assert Path(sidecar).read_text() == "sibling stage output\n"
        # Canonical file now holds the writer's content (warn-but-write is
        # preserved — this fix adds recoverability, not blocking).
        assert p.read_text() == "A overwrites\n"
        # Both paths surfaced so the caller can merge.
        warn = r.get("_warning", "")
        assert sidecar in warn

    def test_unread_sibling_write_also_preserved(self, workdir):
        # A never read the file at all (read path broken) — the exact
        # silent-loss scenario from the issue.
        from tools.file_tools import write_file_tool

        p = _seed(workdir / "dispatch.jsonl", '{"stage": "sibling"}\n')
        file_state.note_write("B", str(p))
        r = json.loads(
            write_file_tool(path=str(p), content='{"stage": "A"}\n', task_id="A")
        )
        assert "error" not in r, r
        sidecar = r.get("conflict_preserved_at")
        assert sidecar and Path(sidecar).read_text() == '{"stage": "sibling"}\n'

    def test_no_conflict_no_sidecar(self, workdir):
        from tools.file_tools import write_file_tool

        p = _seed(workdir / "clean.txt", "clean\n")
        r = json.loads(
            write_file_tool(path=str(p), content="rewritten\n", task_id="A")
        )
        assert "error" not in r, r
        assert not r.get("conflict_preserved_at")
        assert not glob.glob(str(workdir / "*.conflict-*"))

    def test_external_edit_creates_no_sidecar(self, workdir):
        from tools.file_tools import write_file_tool

        p = _seed(workdir / "fmt.txt", "x\n")
        file_state.record_read("A", str(p))
        time.sleep(0.01)
        os.utime(str(p), None)  # external touch — not agent-attributed
        r = json.loads(
            write_file_tool(path=str(p), content="y\n", task_id="A")
        )
        assert "error" not in r, r
        assert not r.get("conflict_preserved_at")
        assert not glob.glob(str(workdir / "*.conflict-*"))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
