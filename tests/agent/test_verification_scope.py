# -*- coding: utf-8 -*-
"""Unit tests for verification scope boundary enforcement (#2436)."""

from pathlib import Path
import pytest

from agent.verification_scope import (
    ScopeViolation,
    VerificationScope,
    VerificationScopeEnforcer,
    get_stage_verification_scope,
)


class TestVerificationScope:
    """Test suite for VerificationScope and VerificationScopeEnforcer."""

    def test_scope_serialization(self):
        scope = VerificationScope(
            allowed_paths=["/workspace"],
            denied_paths=["/workspace/.git/secrets"],
            allowed_commands=["git status", "pytest"],
            denied_commands=["rm -rf *"],
            allow_network=False,
            read_only=True,
            name="test_scope",
        )
        d = scope.to_dict()
        assert d["name"] == "test_scope"
        assert d["read_only"] is True

        restored = VerificationScope.from_dict(d)
        assert restored.name == scope.name
        assert restored.allowed_paths == scope.allowed_paths
        assert restored.allow_network is False

    def test_file_access_read_only_enforcement(self, tmp_path: Path):
        scope = VerificationScope(
            allowed_paths=[str(tmp_path)],
            read_only=True,
            name="read_only_scope",
        )
        enforcer = VerificationScopeEnforcer(scope)

        test_file = tmp_path / "data.txt"
        test_file.write_text("hello", encoding="utf-8")

        # Read allowed
        ok, violation = enforcer.check_file_access(test_file, mode="read")
        assert ok is True
        assert violation is None

        # Write blocked
        ok_w, viol_w = enforcer.check_file_access(test_file, mode="write")
        assert ok_w is False
        assert viol_w is not None
        assert viol_w.action_type == "file_write"
        assert "read-only" in viol_w.reason

    def test_file_access_allowed_and_denied_paths(self, tmp_path: Path):
        allowed_dir = tmp_path / "project"
        allowed_dir.mkdir()
        denied_dir = allowed_dir / ".git" / "hooks"
        denied_dir.mkdir(parents=True)
        outside_dir = tmp_path / "outside"
        outside_dir.mkdir()

        scope = VerificationScope(
            allowed_paths=[str(allowed_dir)],
            denied_paths=[str(denied_dir)],
            read_only=False,
        )
        enforcer = VerificationScopeEnforcer(scope)

        # File in allowed directory
        ok1, v1 = enforcer.check_file_access(allowed_dir / "src.py", mode="write")
        assert ok1 is True
        assert v1 is None

        # File in denied directory inside allowed directory
        ok2, v2 = enforcer.check_file_access(denied_dir / "pre-commit", mode="write")
        assert ok2 is False
        assert v2 is not None
        assert "denied" in v2.reason

        # File outside allowed boundaries
        ok3, v3 = enforcer.check_file_access(outside_dir / "secret.txt", mode="read")
        assert ok3 is False
        assert v3 is not None
        assert "outside" in v3.reason

    def test_command_execution_enforcement(self):
        scope = VerificationScope(
            allowed_commands=["git status", "pytest*", "cargo test"],
            denied_commands=["*rm -rf*", "sudo *"],
        )
        enforcer = VerificationScopeEnforcer(scope)

        # Allowed commands
        assert enforcer.check_command_execution("git status")[0] is True
        assert enforcer.check_command_execution("pytest tests/agent/")[0] is True

        # Denied command matching denied pattern
        ok_denied, v_denied = enforcer.check_command_execution("rm -rf /tmp/data")
        assert ok_denied is False
        assert v_denied is not None
        assert "denied" in v_denied.reason

        # Command not in allowed list
        ok_unallowed, v_unallowed = enforcer.check_command_execution(
            "npm install evil-pkg"
        )
        assert ok_unallowed is False
        assert v_unallowed is not None
        assert "allowed" in v_unallowed.reason

    def test_network_access_enforcement(self):
        scope = VerificationScope(
            allow_network=True,
            allowed_hosts=["github.com", "arxiv.org"],
        )
        enforcer = VerificationScopeEnforcer(scope)

        assert (
            enforcer.check_network_access("https://arxiv.org/abs/2608.11949")[0] is True
        )
        assert (
            enforcer.check_network_access("https://api.github.com/repos")[0] is True
        )  # subdomain of github.com
        assert (
            enforcer.check_network_access("https://unauthorized.evil.org/data")[0]
            is False
        )

        ok_blocked, v_blocked = enforcer.check_network_access(
            "https://malicious-c2.example.com/exfil"
        )
        assert ok_blocked is False
        assert v_blocked is not None
        assert "not in allowed hosts" in v_blocked.reason

        # Disabled network
        scope_no_net = VerificationScope(allow_network=False)
        enforcer_no_net = VerificationScopeEnforcer(scope_no_net)
        ok_no_net, v_no_net = enforcer_no_net.check_network_access("https://arxiv.org")
        assert ok_no_net is False
        assert v_no_net is not None

    def test_preset_stage_scopes(self, tmp_path: Path):
        research_scope = get_stage_verification_scope("research", tmp_path)
        assert research_scope.read_only is True
        assert research_scope.allow_network is True
        assert "arxiv.org" in research_scope.allowed_hosts

        analysis_scope = get_stage_verification_scope("analysis", tmp_path)
        assert analysis_scope.read_only is True
        assert analysis_scope.allow_network is False

        impl_scope = get_stage_verification_scope("implementation", tmp_path)
        assert impl_scope.read_only is False
        assert impl_scope.allow_network is True
