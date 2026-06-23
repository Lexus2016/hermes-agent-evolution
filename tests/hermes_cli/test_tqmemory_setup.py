"""Tests for hermes_cli.tqmemory_setup.

Covers the pure logic — canonical ``mcp_servers`` schema, idempotency, stale-path
repair, respect for a user-disabled entry, multi-profile registration, opt-out
channels, and the reconcile gating — without ever touching the network or uv.
"""

import pytest
import yaml

import hermes_cli.tqmemory_setup as tqm

BIN = "/abs/path/turbo-memory-mcp"


def _read(p) -> dict:
    return yaml.safe_load(p.read_text(encoding="utf-8"))


class TestRegisterInConfigFile:
    def test_writes_canonical_schema_to_fresh_config(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", "/tmp/hermes-home-test")
        cfg = tmp_path / "config.yaml"
        changed = tqm._register_in_config_file(cfg, BIN)
        assert changed is True
        entry = _read(cfg)["mcp_servers"]["tqmemory"]
        # The RC1b regression guard: env must be present AND args == ["serve"].
        assert entry["command"] == BIN
        assert entry["args"] == ["serve"]
        # Stable project root pins project_id (cwd-independent); migrate flag stays.
        assert entry["env"] == {
            "TQMEMORY_MIGRATE_ON_STARTUP": "1",
            "TQMEMORY_PROJECT_ROOT": "/tmp/hermes-home-test",
        }
        # Generous per-server timeout for the first ~600MB embedding-model load.
        assert entry["timeout"] == 600
        assert entry["enabled"] is True

    def test_idempotent_second_call_is_noop(self, tmp_path):
        cfg = tmp_path / "config.yaml"
        assert tqm._register_in_config_file(cfg, BIN) is True
        assert tqm._register_in_config_file(cfg, BIN) is False

    def test_repairs_stale_command_path(self, tmp_path):
        cfg = tmp_path / "config.yaml"
        cfg.write_text(yaml.safe_dump({
            "mcp_servers": {
                "tqmemory": {"command": "/old/bad", "args": ["serve"], "enabled": True}
            }
        }), encoding="utf-8")
        assert tqm._register_in_config_file(cfg, BIN) is True
        assert _read(cfg)["mcp_servers"]["tqmemory"]["command"] == BIN

    def test_respects_user_disabled_entry(self, tmp_path):
        cfg = tmp_path / "config.yaml"
        cfg.write_text(yaml.safe_dump({
            "mcp_servers": {
                "tqmemory": {"command": BIN, "args": ["serve"], "enabled": False}
            }
        }), encoding="utf-8")
        assert tqm._register_in_config_file(cfg, BIN) is False
        assert _read(cfg)["mcp_servers"]["tqmemory"]["enabled"] is False

    def test_preserves_other_keys_and_servers(self, tmp_path):
        cfg = tmp_path / "config.yaml"
        cfg.write_text(yaml.safe_dump({
            "model": {"default": "kimi"},
            "mcp_servers": {"other": {"command": "/x", "enabled": True}},
        }), encoding="utf-8")
        tqm._register_in_config_file(cfg, BIN)
        data = _read(cfg)
        assert data["model"] == {"default": "kimi"}
        assert "other" in data["mcp_servers"]
        assert "tqmemory" in data["mcp_servers"]

    def test_does_not_clobber_non_mapping_config(self, tmp_path):
        cfg = tmp_path / "config.yaml"
        cfg.write_text("- just\n- a list\n", encoding="utf-8")
        assert tqm._register_in_config_file(cfg, BIN) is False
        assert isinstance(yaml.safe_load(cfg.read_text(encoding="utf-8")), list)

    def test_fresh_create_stamps_config_version(self, tmp_path):
        # A brand-new config must carry _config_version so a fresh profile is not
        # flagged "v0 → N outdated" by doctor/desktop.
        from hermes_cli.config import DEFAULT_CONFIG
        cfg = tmp_path / "config.yaml"
        assert tqm._register_in_config_file(cfg, BIN) is True
        data = _read(cfg)
        assert data["_config_version"] == DEFAULT_CONFIG.get("_config_version")

    def test_existing_versionless_config_not_stamped(self, tmp_path):
        # We must NOT claim a legacy (versionless) config is current — that is the
        # migration pipeline's call, not ours.
        cfg = tmp_path / "config.yaml"
        cfg.write_text(yaml.safe_dump({"model": {"default": "kimi"}}), encoding="utf-8")
        assert tqm._register_in_config_file(cfg, BIN) is True
        assert "_config_version" not in _read(cfg)

    def test_repairs_missing_env(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", "/tmp/hermes-home-test")
        cfg = tmp_path / "config.yaml"
        cfg.write_text(yaml.safe_dump({
            "mcp_servers": {"tqmemory": {"command": BIN, "args": ["serve"], "enabled": True}}
        }), encoding="utf-8")
        assert tqm._register_in_config_file(cfg, BIN) is True
        env = _read(cfg)["mcp_servers"]["tqmemory"]["env"]
        # Repair back-fills BOTH the migrate flag and the stable project root so
        # existing client installs heal on `hermes update`.
        assert env == {
            "TQMEMORY_MIGRATE_ON_STARTUP": "1",
            "TQMEMORY_PROJECT_ROOT": "/tmp/hermes-home-test",
        }

    def test_fully_correct_entry_is_noop(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", "/tmp/hermes-home-test")
        cfg = tmp_path / "config.yaml"
        cfg.write_text(yaml.safe_dump({
            "mcp_servers": {"tqmemory": {
                "command": BIN, "args": ["serve"],
                "env": {
                    "TQMEMORY_MIGRATE_ON_STARTUP": "1",
                    "TQMEMORY_PROJECT_ROOT": "/tmp/hermes-home-test",
                },
                "timeout": 600, "enabled": True,
            }}
        }), encoding="utf-8")
        assert tqm._register_in_config_file(cfg, BIN) is False


class TestEnsureRegisteredMultiProfile:
    def test_registers_into_all_profiles(self, tmp_path, monkeypatch):
        c1 = tmp_path / "default" / "config.yaml"
        c2 = tmp_path / "user1" / "config.yaml"
        monkeypatch.setattr(tqm, "_all_profile_config_paths", lambda: [c1, c2])
        monkeypatch.setattr(tqm, "_is_managed", lambda: False)
        assert tqm.ensure_tqmemory_registered(BIN, all_profiles=True, quiet=True) == 2
        for c in (c1, c2):
            assert _read(c)["mcp_servers"]["tqmemory"]["command"] == BIN

    def test_skips_when_managed(self, monkeypatch):
        monkeypatch.setattr(tqm, "_is_managed", lambda: True)
        monkeypatch.setattr(
            tqm, "_all_profile_config_paths",
            lambda: pytest.fail("must not enumerate profiles on a managed install"),
        )
        assert tqm.ensure_tqmemory_registered(BIN, all_profiles=True, quiet=True) == 0


class TestOptOut:
    def test_env_var(self, monkeypatch):
        monkeypatch.setenv(tqm.OPT_OUT_ENV, "1")
        assert tqm.opted_out() is True

    def test_config_flag(self, monkeypatch):
        monkeypatch.delenv(tqm.OPT_OUT_ENV, raising=False)
        monkeypatch.setattr(
            "hermes_cli.config.read_raw_config",
            lambda: {"memory": {"tqmemory_autoinstall": False}},
        )
        assert tqm.opted_out() is True

    def test_default_not_opted_out(self, monkeypatch):
        monkeypatch.delenv(tqm.OPT_OUT_ENV, raising=False)
        monkeypatch.setattr("hermes_cli.config.read_raw_config", lambda: {})
        assert tqm.opted_out() is False


class TestReconcileGating:
    def test_optout_short_circuits_before_install(self, monkeypatch):
        monkeypatch.setattr(tqm, "opted_out", lambda: True)
        monkeypatch.setattr(
            tqm, "ensure_turbo_memory_installed",
            lambda **k: pytest.fail("must not install when opted out"),
        )
        assert tqm.reconcile_tqmemory(quiet=True) == (False, 0)

    def test_once_guard_short_circuits(self, monkeypatch):
        monkeypatch.setattr(tqm, "_reconciled_this_process", True)
        monkeypatch.setattr(
            tqm, "ensure_turbo_memory_installed",
            lambda **k: pytest.fail("must not run when once=True and already reconciled"),
        )
        assert tqm.reconcile_tqmemory(quiet=True, once=True) == (True, 0)

    def test_full_reconcile_installs_registers_verifies(self, tmp_path, monkeypatch):
        cfg = tmp_path / "config.yaml"
        monkeypatch.setattr(tqm, "_reconciled_this_process", False)
        monkeypatch.setattr(tqm, "opted_out", lambda: False)
        monkeypatch.setattr(tqm, "_is_managed", lambda: False)
        monkeypatch.setattr(tqm, "ensure_turbo_memory_installed", lambda **k: BIN)
        monkeypatch.setattr(tqm, "_all_profile_config_paths", lambda: [cfg])
        monkeypatch.setattr(tqm, "verify_tqmemory", lambda *a, **k: True)
        installed, changed = tqm.reconcile_tqmemory(quiet=True)
        assert installed is True
        assert changed == 1
        assert _read(cfg)["mcp_servers"]["tqmemory"]["command"] == BIN


class TestRegisterForProfile:
    """Single-profile, register-only path used at profile-creation time."""

    def _patch_ready(self, monkeypatch):
        monkeypatch.setattr(tqm, "opted_out", lambda: False)
        monkeypatch.setattr(tqm, "_is_managed", lambda: False)
        monkeypatch.setattr(tqm, "resolve_binary", lambda: BIN)

    def test_registers_into_new_profile_config(self, tmp_path, monkeypatch):
        self._patch_ready(monkeypatch)
        cfg = tmp_path / "config.yaml"
        assert tqm.register_for_profile(cfg) is True
        assert _read(cfg)["mcp_servers"]["tqmemory"]["command"] == BIN

    def test_noop_when_opted_out(self, tmp_path, monkeypatch):
        self._patch_ready(monkeypatch)
        monkeypatch.setattr(tqm, "opted_out", lambda: True)
        cfg = tmp_path / "config.yaml"
        assert tqm.register_for_profile(cfg) is False
        assert not cfg.exists()

    def test_noop_when_managed(self, tmp_path, monkeypatch):
        self._patch_ready(monkeypatch)
        monkeypatch.setattr(tqm, "_is_managed", lambda: True)
        cfg = tmp_path / "config.yaml"
        assert tqm.register_for_profile(cfg) is False
        assert not cfg.exists()

    def test_noop_when_binary_missing(self, tmp_path, monkeypatch):
        self._patch_ready(monkeypatch)
        monkeypatch.setattr(tqm, "resolve_binary", lambda: None)
        cfg = tmp_path / "config.yaml"
        assert tqm.register_for_profile(cfg) is False
        assert not cfg.exists()
