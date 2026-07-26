"""Tests for scripts/evolution_hydra_gate.py — wake-gate that checks upstream
freshness, GitHub write access, the pipeline halt-state, and the per-edge
dispatch ledger (#1305) that suppresses false-positive re-wake-ups."""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
GATE = REPO_ROOT / "scripts" / "evolution_hydra_gate.py"
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import evolution_hydra_gate as hydra  # noqa: E402


class TestCheckHalt:
    def test_halt_file_exists(self, tmp_path):
        halt_file = tmp_path / "halt-state.txt"
        halt_file.write_text("HALTED\n", encoding="utf-8")
        halted, returned_path = hydra._check_halt(tmp_path)
        assert halted is True
        assert returned_path == halt_file

    def test_no_halt_file(self, tmp_path):
        halted, returned_path = hydra._check_halt(tmp_path)
        assert halted is False
        assert returned_path == tmp_path / "halt-state.txt"

    def test_oserror_fail_open(self, tmp_path):
        # A path that raises on exists() should be treated as not-halted.
        class BadPath(type(tmp_path)):
            def exists(self):
                raise OSError("permission denied")

        bad_path = BadPath(tmp_path)
        halted, returned_path = hydra._check_halt(bad_path)
        assert halted is False


class TestMainHalt:
    def test_halt_file_prevents_wake_even_with_fresh_material(
        self, tmp_path, capsys, monkeypatch
    ):
        """A set halt-state must suppress the agent regardless of pool freshness."""
        monkeypatch.setenv("EVOLUTION_PROFILE_DIR", str(tmp_path))
        (tmp_path / "halt-state.txt").write_text("HALTED\n", encoding="utf-8")
        # Create fresh upstream material so the gate would otherwise wake.
        (tmp_path / "research").mkdir()
        (tmp_path / "research" / "2026-07-07.json").write_text("{}", encoding="utf-8")

        with patch.object(
            hydra, "_check_github_write_access", return_value=(True, "ok")
        ):
            rc = hydra.main()

        assert rc == 0
        out = capsys.readouterr().out
        assert "HALTED" in out
        last_line = out.strip().splitlines()[-1]
        assert json.loads(last_line) == {"wakeAgent": False}


class TestDispatchLedger:
    """The per-edge dispatch ledger (#1305) — a stage already dispatched today
    must NOT re-wake the orchestrator on a subsequent tick unless the upstream
    stage produced strictly newer output."""

    def _make_edge_fresh(self, tmp_path: Path, edge: str):
        """Create upstream-but-not-downstream output for a given edge label so
        ``_check_pool`` reports the edge as fresh. Uses the real pair mapping."""
        up_for = {
            "research→issues": ("research", "issues"),
            "issues→analysis": ("issues", "analysis"),
            "introspection→analysis": ("introspection", "analysis"),
            "analysis→implementation": ("analysis", "implementation"),
            "implementation→integration": ("implementation", "integration"),
            "integration→upstream-sync": ("integration", "upstream-sync"),
        }
        up, down = up_for[edge]
        up_dir = tmp_path / up
        up_dir.mkdir(parents=True, exist_ok=True)
        (up_dir / f"{hydra._today()}.json").write_text("{}", encoding="utf-8")
        # downstream dir absent → edge is "fresh" (down_mtime == 0).

    def test_ledger_round_trip(self, tmp_path):
        """_read_ledger returns what _write_ledger persisted; corrupt/missing
        files read back as {} (fail-open)."""
        assert hydra._read_ledger(tmp_path) == {}  # missing file
        hydra._write_ledger(tmp_path, {"x": {"date": "2026-01-01"}})
        assert hydra._read_ledger(tmp_path) == {"x": {"date": "2026-01-01"}}
        # corrupt JSON → fail-open to {}
        hydra._ledger_path(tmp_path).write_text("{not json", encoding="utf-8")
        assert hydra._read_ledger(tmp_path) == {}

    def test_already_dispatched_false_when_no_entry(self, tmp_path):
        assert hydra._already_dispatched_today(tmp_path, "edge", 100.0) is False

    def test_already_dispatched_false_for_stale_date(self, tmp_path):
        """A ledger entry from a prior day does not suppress today's tick."""
        hydra._write_ledger(
            tmp_path,
            {"edge": {"date": "1999-01-01", "upstream_mtime": 100.0}},
        )
        assert hydra._already_dispatched_today(tmp_path, "edge", 100.0) is False

    def test_already_dispatched_true_for_same_mtime_today(self, tmp_path):
        """Same upstream mtime, same day → suppress (the core false-positive fix)."""
        hydra._write_ledger(
            tmp_path,
            {"edge": {"date": hydra._today(), "upstream_mtime": 100.0}},
        )
        assert hydra._already_dispatched_today(tmp_path, "edge", 100.0) is True

    def test_already_dispatched_false_for_newer_upstream(self, tmp_path):
        """Strictly newer upstream mtime → genuine new material → re-fire."""
        hydra._write_ledger(
            tmp_path,
            {"edge": {"date": hydra._today(), "upstream_mtime": 100.0}},
        )
        assert hydra._already_dispatched_today(tmp_path, "edge", 200.0) is False

    def test_first_tick_wakes_and_records_dispatch(self, tmp_path, capsys, monkeypatch):
        """First fresh tick wakes the orchestrator AND writes a ledger entry."""
        monkeypatch.setenv("EVOLUTION_PROFILE_DIR", str(tmp_path))
        self._make_edge_fresh(tmp_path, "integration→upstream-sync")
        with patch.object(
            hydra, "_check_github_write_access", return_value=(True, "ok")
        ):
            rc = hydra.main()
        assert rc == 0
        out = capsys.readouterr().out
        assert "fresh material" in out
        assert json.loads(out.strip().splitlines()[-1]) == {"wakeAgent": True}
        # Ledger now has an entry for the edge, dated today.
        ledger = hydra._read_ledger(tmp_path)
        assert "integration→upstream-sync" in ledger
        assert ledger["integration→upstream-sync"]["date"] == hydra._today()

    def test_second_tick_same_day_same_mtime_suppressed(
        self, tmp_path, capsys, monkeypatch
    ):
        """The bug from #1305: a second tick on the same day with NO upstream
        advancement must NOT re-wake the orchestrator on the SAME edge. This is
        the regression test for the 20+/day false-positive wake-ups.

        (A wake for an UNRELATED reason — e.g. a time trigger on a different
        stage — is fine and out of scope for this test; the fix targets
        repeat-dispatch of an already-adjudicated edge.)"""
        monkeypatch.setenv("EVOLUTION_PROFILE_DIR", str(tmp_path))
        self._make_edge_fresh(tmp_path, "integration→upstream-sync")
        edge = "integration→upstream-sync"
        with patch.object(
            hydra, "_check_github_write_access", return_value=(True, "ok")
        ):
            hydra.main()  # tick 1 — wakes on the edge + records dispatch
            out1 = capsys.readouterr().out
            assert edge in out1
            assert json.loads(out1.strip().splitlines()[-1]) == {"wakeAgent": True}

            hydra.main()  # tick 2 — same day, same mtime
            out2 = capsys.readouterr().out
        last2 = json.loads(out2.strip().splitlines()[-1])
        if last2["wakeAgent"]:
            # If it wakes, it must NOT be a repeat-dispatch of the same edge.
            # The suppression is verified by the edge being absent from the
            # "fresh material" reason (either it sleeps, or wakes for a
            # different reason like a time trigger).
            assert edge not in out2, (
                f"edge {edge} re-fired on tick 2 with no upstream advancement "
                f"(reason: {out2.strip()!r})"
            )
        else:
            # It slept — and the reason should note the suppression.
            assert "already dispatched today" in out2

    def test_newer_upstream_after_dispatch_re_fires(
        self, tmp_path, capsys, monkeypatch
    ):
        """Genuine new material (newer upstream mtime) must re-wake even after a
        same-day dispatch — the ledger must not suppress a real state change."""
        monkeypatch.setenv("EVOLUTION_PROFILE_DIR", str(tmp_path))
        self._make_edge_fresh(tmp_path, "integration→upstream-sync")
        with patch.object(
            hydra, "_check_github_write_access", return_value=(True, "ok")
        ):
            hydra.main()  # tick 1 — records dispatch at mtime T1
            capsys.readouterr()

            # Simulate the upstream stage producing NEW output (newer mtime).
            up_file = tmp_path / "integration" / f"{hydra._today()}.json"
            new_time = time.time()
            os.utime(up_file, (new_time, new_time))

            hydra.main()  # tick 2 — newer upstream → must wake
            out2 = capsys.readouterr().out
        assert "fresh material" in out2
        assert json.loads(out2.strip().splitlines()[-1]) == {"wakeAgent": True}

    def test_ledger_prunes_old_entries(self, tmp_path):
        """_record_dispatch prunes entries older than the prune window so the
        file does not grow unbounded over months."""
        # Seed with one very old entry and one from today.
        old = {
            "ancient-edge": {"date": "2000-01-01", "upstream_mtime": 1.0},
            "fresh-edge": {"date": hydra._today(), "upstream_mtime": 5.0},
        }
        hydra._write_ledger(tmp_path, old)
        # Recording a new dispatch should drop the ancient entry.
        hydra._record_dispatch(tmp_path, "new-edge", 10.0)
        ledger = hydra._read_ledger(tmp_path)
        assert "ancient-edge" not in ledger
        assert "fresh-edge" in ledger
        assert "new-edge" in ledger

    def test_corrupt_ledger_does_not_block_wake(self, tmp_path, capsys, monkeypatch):
        """A corrupt ledger must fail-open: the first fresh tick still wakes the
        orchestrator (never suppress a genuine wake-up due to a corrupt file)."""
        monkeypatch.setenv("EVOLUTION_PROFILE_DIR", str(tmp_path))
        self._make_edge_fresh(tmp_path, "integration→upstream-sync")
        hydra._ledger_path(tmp_path).write_text("{broken json", encoding="utf-8")
        with patch.object(
            hydra, "_check_github_write_access", return_value=(True, "ok")
        ):
            hydra.main()
            out = capsys.readouterr().out
        assert json.loads(out.strip().splitlines()[-1]) == {"wakeAgent": True}
