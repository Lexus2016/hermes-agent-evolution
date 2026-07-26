"""Tests for scripts/evolution_hydra_gate.py — wake-gate that checks upstream
freshness, GitHub write access, and the pipeline halt-state."""

from __future__ import annotations

import json
import os
import sys
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
    """#1305 — a pair adjudicated STEADY_STATE/CONSUMED today must not re-wake."""

    def _write_ledger(self, evo_dir: Path, records):
        ledger = evo_dir / f"hydra-dispatch-{hydra._today()}.jsonl"
        with ledger.open("w", encoding="utf-8") as fh:
            for rec in records:
                fh.write(json.dumps(rec) + "\n")

    def test_terminal_verdict_suppresses_pair(self, tmp_path):
        """integration→upstream-sync resolved STEADY_STATE today → not fresh."""
        self._write_ledger(
            tmp_path,
            [{"stage": "integration→upstream-sync", "verdict": "STEADY_STATE"}],
        )
        terminal = hydra._load_terminal_verdicts(tmp_path)
        assert terminal.get("integration→upstream-sync") == "STEADY_STATE"
        pool = hydra._check_pool(tmp_path)
        assert pool["integration→upstream-sync"] is False

    def test_consumed_verdict_also_suppresses(self, tmp_path):
        self._write_ledger(
            tmp_path,
            [{"edge": "analysis→implementation", "verdict": "CONSUMED"}],
        )
        terminal = hydra._load_terminal_verdicts(tmp_path)
        assert terminal.get("analysis→implementation") == "CONSUMED"

    def test_non_terminal_verdict_does_not_suppress(self, tmp_path):
        """A non-terminal verdict (e.g. WORK_NEEDED) must NOT suppress the pair."""
        self._write_ledger(
            tmp_path,
            [{"stage": "integration→upstream-sync", "verdict": "WORK_NEEDED"}],
        )
        terminal = hydra._load_terminal_verdicts(tmp_path)
        assert "integration→upstream-sync" not in terminal

    def test_missing_ledger_is_empty(self, tmp_path):
        """No ledger file → no terminal verdicts (fail open)."""
        assert hydra._load_terminal_verdicts(tmp_path) == {}

    def test_malformed_ledger_line_skipped(self, tmp_path):
        """Malformed lines must not crash the gate."""
        ledger = tmp_path / f"hydra-dispatch-{hydra._today()}.jsonl"
        ledger.write_text(
            "not json\n"
            + json.dumps({"verdict": "STEADY_STATE"})  # no stage/edge → skip
            + "\n"
            + json.dumps({"stage": "integration→upstream-sync", "verdict": "CONSUMED"})
            + "\n",
            encoding="utf-8",
        )
        terminal = hydra._load_terminal_verdicts(tmp_path)
        assert terminal == {"integration→upstream-sync": "CONSUMED"}

    def test_main_suppresses_wake_when_only_terminal_pair_would_fire(
        self, tmp_path, capsys, monkeypatch
    ):
        """End-to-end: only the integration→upstream-sync pair has fresh upstream
        material, but it was adjudicated STEADY_STATE today → gate sleeps.

        All time-triggers (research, introspection, upstream-sync) must have
        recent output so they don't independently wake the gate.
        """
        monkeypatch.setenv("EVOLUTION_PROFILE_DIR", str(tmp_path))
        today = hydra._today()
        # Satisfy every stage so no time-trigger fires.
        for stage in (
            "research",
            "issues",
            "introspection",
            "analysis",
            "implementation",
            "integration",
            "upstream-sync",
        ):
            (tmp_path / stage).mkdir()
            (tmp_path / stage / f"{today}.json").write_text("{}", encoding="utf-8")
        # Make upstream (integration) fresher than downstream (upstream-sync) —
        # this pair would normally wake.
        down = tmp_path / "upstream-sync" / f"{today}.json"
        import os, time

        old_ts = time.time() - 3600
        os.utime(down, (old_ts, old_ts))
        # Ledger says this pair is already settled today.
        ledger = tmp_path / f"hydra-dispatch-{today}.jsonl"
        ledger.write_text(
            json.dumps({
                "stage": "integration→upstream-sync",
                "verdict": "STEADY_STATE",
            })
            + "\n",
            encoding="utf-8",
        )
        with patch.object(
            hydra, "_check_github_write_access", return_value=(True, "ok")
        ):
            rc = hydra.main()
        assert rc == 0
        out = capsys.readouterr().out
        last_line = out.strip().splitlines()[-1]
        assert json.loads(last_line) == {"wakeAgent": False}
