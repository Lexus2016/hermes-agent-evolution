"""Tests for evolution_disable_failed_mcp.py (#1541)."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from evolution_disable_failed_mcp import (  # noqa: E402
    check_tqmemory_health,
    disable_in_profiles,
    main,
)


def _mock_mod(**kwargs):
    m = MagicMock()
    m.resolve_binary = MagicMock(return_value=kwargs.get("binary"))
    m.verify_tqmemory = MagicMock(return_value=kwargs.get("verify", False))
    m._all_profile_config_paths = kwargs.get("paths", MagicMock(return_value=[]))
    m.SERVER_NAME = "tqmemory"
    return m


class TestCheckHealth:
    def test_binary_not_found(self):
        with patch(
            "evolution_disable_failed_mcp._import_tqmemory",
            return_value=_mock_mod(binary=None),
        ):
            assert check_tqmemory_health()[0] is False

    def test_binary_healthy(self):
        mod = _mock_mod(binary="/usr/bin/tqm", verify=True)
        with patch("evolution_disable_failed_mcp._import_tqmemory", return_value=mod):
            assert check_tqmemory_health()[0] is True

    def test_binary_verify_fails(self):
        mod = _mock_mod(binary="/usr/bin/tqm", verify=False)
        with patch("evolution_disable_failed_mcp._import_tqmemory", return_value=mod):
            healthy, msg = check_tqmemory_health()
        assert healthy is False and "failed" in msg.lower()


class TestDisableInProfiles:
    def test_already_disabled(self, tmp_path):
        import yaml

        cfg = tmp_path / "config.yaml"
        cfg.write_text(yaml.dump({"mcp_servers": {"tqmemory": {"enabled": False}}}))
        mod = _mock_mod(paths=MagicMock(return_value=[cfg]))
        with patch("evolution_disable_failed_mcp._import_tqmemory", return_value=mod):
            assert disable_in_profiles(dry_run=False) == []


class TestMain:
    def test_healthy_no_action(self, capsys):
        with patch(
            "evolution_disable_failed_mcp.check_tqmemory_health",
            return_value=(True, "ok"),
        ):
            assert main([]) == 0
        assert "no action needed" in capsys.readouterr().out

    def test_broken_disables(self, capsys):
        with patch(
            "evolution_disable_failed_mcp.check_tqmemory_health",
            return_value=(False, "missing"),
        ):
            with patch(
                "evolution_disable_failed_mcp.disable_in_profiles",
                return_value=["/tmp/cfg.yaml"],
            ):
                assert main([]) == 0
        assert "disabled in" in capsys.readouterr().out
