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
