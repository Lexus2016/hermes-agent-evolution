"""Sandbox-escape regression tests for the tool-execution boundary (#2641).

Maps SandboxEscapeBench layers (orchestration / runtime / kernel, arXiv:2603.02277)
onto real Hermes boundary code — see docs/evolution/sandbox-escape-threat-model.md.
Each test exercises the actual module; no mock sandbox is invented.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from unittest import mock

import pytest

from agent import file_safety
from agent import secret_scope
from agent.proxy_sources.iron_proxy import TokenMapping
from agent.proxy_sources.iron_proxy import build_proxy_config
from tools.environments.local import LocalEnvironment


class TestSandboxEscapeBoundary:
    """Escapes must fail at Hermes's real tool-execution boundary."""

    # ── orchestration layer: filesystem escape ─────────────────────────
    def test_filesystem_escape_is_write_denied(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """System files and ~/.ssh keys are hard-denied; temp HOME stays writable."""
        monkeypatch.delenv("HERMES_WRITE_SAFE_ROOT", raising=False)
        home = str(Path.home())

        denied = file_safety.build_write_denied_paths(home)
        assert "/etc/passwd" in denied
        assert "/etc/shadow" in denied

        for escape in ("/etc/passwd", "/etc/shadow", "/etc/sudoers"):
            assert file_safety.is_write_denied(escape), escape
        assert file_safety.is_write_denied(os.path.join(home, ".ssh", "id_rsa"))

        inside = tmp_path / "sandbox" / "notes.txt"
        assert not file_safety.is_write_denied(str(inside))

    # ── orchestration layer: env / secret exfiltration ─────────────────
    def test_secret_scope_never_exposes_foreign_secrets(self, monkeypatch) -> None:
        """Multiplex-mode get_secret is scope-allowlisted and fail-closed."""
        monkeypatch.setenv("PROFILE_B_API_KEY", "sk-other-profile")
        secret_scope.set_multiplex_active(True)
        token = secret_scope.set_secret_scope({"PROFILE_A_API_KEY": "sk-a"})
        try:
            assert secret_scope.get_secret("PROFILE_A_API_KEY") == "sk-a"
            assert (
                secret_scope.get_secret("PROFILE_B_API_KEY", default="__missing__")
                == "__missing__"
            )
        finally:
            secret_scope.reset_secret_scope(token)

        try:
            with pytest.raises(secret_scope.UnscopedSecretError):
                secret_scope.get_secret("PROFILE_B_API_KEY")
            # allowlisted globals (deployment config) still read os.environ
            assert secret_scope.get_secret("HERMES_HOME") is not None
        finally:
            secret_scope.set_multiplex_active(False)

    # ── runtime layer: process spawn encoding ──────────────────────────
    def test_process_spawn_passes_explicit_encoding(self, tmp_path: Path) -> None:
        """Main spawn path (_run_bash) pins utf-8/errors=replace on Popen."""
        seen: dict = {}
        real_popen = subprocess.Popen

        def recording_popen(*args, **kwargs):
            seen.update(kwargs)
            return real_popen(*args, **kwargs)

        env = LocalEnvironment(cwd=str(tmp_path), timeout=30)
        try:
            with mock.patch(
                "tools.environments.local.subprocess.Popen",
                side_effect=recording_popen,
            ):
                result = env.execute("printf 'escape-probe'")
        finally:
            env.cleanup()

        assert result["returncode"] == 0
        assert "escape-probe" in result["output"]
        assert seen.get("encoding") == "utf-8"
        assert seen.get("errors") == "replace"

    # ── kernel layer: network egress ───────────────────────────────────
    def test_network_egress_default_deny(self, tmp_path: Path) -> None:
        """Egress proxy config is default-deny: SSRF list, allowlist, fail-closed."""
        cfg = build_proxy_config(
            mappings=[
                TokenMapping(
                    proxy_token="proxy-token",
                    real_env_name="X_API_KEY",
                    upstream_hosts=("api.x.ai",),
                )
            ],
            ca_cert=tmp_path / "ca.crt",
            ca_key=tmp_path / "ca.key",
        )

        deny = cfg["proxy"]["upstream_deny_cidrs"]
        assert "169.254.0.0/16" in deny  # cloud metadata / IMDS
        assert "127.0.0.0/8" in deny  # loopback

        allowlist = cfg["transforms"][0]["config"]["domains"]
        assert "evil.example.com" not in allowlist  # anything else is 403'd
        assert "api.x.ai" in allowlist  # only mapped upstreams are added

        secret = cfg["transforms"][1]["config"]["secrets"][0]
        assert secret["replace"]["require"] is True  # no token swap => reject
