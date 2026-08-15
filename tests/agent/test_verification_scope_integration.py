# -*- coding: utf-8 -*-
"""Integration tests proving verification scope enforcement in real execution path (#2458)."""

import json
from pathlib import Path
import pytest

from agent.file_safety import (
    get_active_verification_scope,
    get_read_block_error,
    get_write_denied_error,
    is_write_denied,
    set_active_verification_scope,
)
from agent.verification_scope import (
    VerificationScope,
    get_stage_verification_scope,
)
from scripts.evolution_orchestrator import build_worker_task
from tools.terminal_tool import terminal_tool


class TestVerificationScopeIntegration:
    """Integration test suite ensuring real call sites enforce verification scope."""

    def teardown_method(self):
        # Reset active verification scope after every test
        set_active_verification_scope(None)

    def test_terminal_tool_blocks_denied_command(self):
        # Without scope: command runs / is evaluated normally
        assert get_active_verification_scope() is None

        # With active scope denying rm commands
        scope = VerificationScope(
            name="guarded_scope",
            denied_commands=["*rm -rf*", "sudo *"],
        )
        set_active_verification_scope(scope)

        res_json = terminal_tool(command="rm -rf /tmp/test_dir")
        res = json.loads(res_json)
        assert res["status"] == "error"
        assert res["exit_code"] == -1
        assert "blocked by active verification scope (guarded_scope)" in res["error"]

    def test_file_safety_enforces_read_only_and_path_bounds(self, tmp_path: Path):
        allowed_dir = tmp_path / "workspace"
        allowed_dir.mkdir()
        outside_file = tmp_path / "secret.txt"
        outside_file.write_text("secret", encoding="utf-8")
        inside_file = allowed_dir / "code.py"
        inside_file.write_text("print(1)", encoding="utf-8")

        scope = VerificationScope(
            name="research_env",
            allowed_paths=[str(allowed_dir)],
            read_only=True,
        )
        set_active_verification_scope(scope)

        # Inside file read: allowed
        assert get_read_block_error(str(inside_file)) is None

        # Inside file write: denied due to read_only
        assert is_write_denied(str(inside_file)) is True
        write_err = get_write_denied_error(str(inside_file))
        assert write_err is not None
        assert "violates active verification scope (research_env)" in write_err

        # Outside file read: denied because outside allowed_paths
        read_err = get_read_block_error(str(outside_file))
        assert read_err is not None
        assert "violates active verification scope (research_env)" in read_err

    def test_orchestrator_attaches_stage_verification_scope(self, tmp_path: Path):
        task = build_worker_task(
            subtask="Analyze memory leaks",
            angle="core loop",
            stage="research",
            workspace_root=str(tmp_path),
        )
        assert "verification_scope" in task
        scope_data = task["verification_scope"]
        assert scope_data["name"] == "research_stage"
        assert scope_data["read_only"] is True
        assert "arxiv.org" in scope_data["allowed_hosts"]
