# -*- coding: utf-8 -*-
"""Tests for self-refining harness state with versioned rollback (Issue #2497, Slice B)."""

from __future__ import annotations

import pytest

from evolution.lib.versioned_harness_state import (
    HarnessVersion,
    VersionedHarnessState,
    get_default_harness_state_dir,
)
from run_agent import AIAgent


class TestVersionedHarnessState:
    def test_init_initial_version(self, tmp_path):
        state = VersionedHarnessState(
            initial_instructions="You are a helpful coding assistant.",
            session_id="test_harness_sess",
            storage_dir=tmp_path,
        )
        assert state.current_version == 1
        assert state.current_instructions == "You are a helpful coding assistant."
        assert len(state.history) == 1
        assert state.history[0].version == 1

    def test_update_version(self, tmp_path):
        state = VersionedHarnessState(
            initial_instructions="Base prompt",
            session_id="test_harness_sess",
            storage_dir=tmp_path,
        )
        v2 = state.update("Revised prompt: be concise", reason="User requested brevity")
        assert v2.version == 2
        assert state.current_version == 2
        assert state.current_instructions == "Revised prompt: be concise"
        assert len(state.history) == 2

    def test_rollback_to_previous_version(self, tmp_path):
        state = VersionedHarnessState(
            initial_instructions="V1 Instructions",
            session_id="test_harness_sess",
            storage_dir=tmp_path,
        )
        state.update("V2 Broken Instructions", reason="Flawed edit")
        assert state.current_version == 2

        # Rollback to previous version (V1)
        v3 = state.rollback(reason="Revert broken instructions")
        assert v3.version == 3
        assert state.current_version == 3
        assert state.current_instructions == "V1 Instructions"
        assert "Rollback to v1" in v3.reason

    def test_rollback_to_specific_version(self, tmp_path):
        state = VersionedHarnessState(
            initial_instructions="Base v1",
            session_id="test_harness_sess",
            storage_dir=tmp_path,
        )
        state.update("Edit v2", reason="step 2")
        state.update("Edit v3", reason="step 3")
        state.update("Edit v4", reason="step 4")

        # Rollback specifically to v2
        v5 = state.rollback(target_version=2, reason="Jump back to v2")
        assert v5.version == 5
        assert state.current_instructions == "Edit v2"

    def test_rollback_invalid_raises(self, tmp_path):
        state = VersionedHarnessState(
            initial_instructions="V1",
            session_id="test_harness_sess",
            storage_dir=tmp_path,
        )
        with pytest.raises(ValueError, match="Cannot rollback"):
            state.rollback()

        state.update("V2")
        with pytest.raises(ValueError, match="Target version 99 does not exist"):
            state.rollback(target_version=99)

    def test_diff_between_versions(self, tmp_path):
        state = VersionedHarnessState(
            initial_instructions="Line 1\nLine 2\n",
            session_id="test_harness_sess",
            storage_dir=tmp_path,
        )
        state.update("Line 1\nLine 2 modified\nLine 3\n", reason="Add line 3")
        diff_text = state.diff(1, 2)
        assert "-Line 2" in diff_text
        assert "+Line 2 modified" in diff_text
        assert "+Line 3" in diff_text

    def test_list_versions_and_metadata(self, tmp_path):
        state = VersionedHarnessState(
            initial_instructions="Init",
            session_id="test_harness_sess",
            storage_dir=tmp_path,
        )
        state.update("Second", reason="second version")
        summary = state.list_versions()
        assert len(summary) == 2
        assert summary[0]["version"] == 1
        assert summary[1]["version"] == 2
        assert summary[1]["reason"] == "second version"

    def test_persistence_and_reload(self, tmp_path):
        state1 = VersionedHarnessState(
            initial_instructions="Saved instructions",
            session_id="persist_harness_sess",
            storage_dir=tmp_path,
        )
        state1.update("Updated instructions", reason="Persist check")

        # Reload from disk in a new instance
        state2 = VersionedHarnessState(
            initial_instructions="Different default",
            session_id="persist_harness_sess",
            storage_dir=tmp_path,
        )
        assert state2.current_version == 2
        assert state2.current_instructions == "Updated instructions"

    def test_execute_command(self, tmp_path):
        state = VersionedHarnessState(
            initial_instructions="CLI init",
            session_id="cli_harness_sess",
            storage_dir=tmp_path,
        )
        up_msg = state.execute_command("update", "CLI updated instructions", "CLI edit")
        assert "Updated harness to v2" in up_msg
        assert state.execute_command("current") == "CLI updated instructions"

        rb_msg = state.execute_command("rollback", "1")
        assert "Rolled back harness to v3" in rb_msg
        assert state.execute_command("current") == "CLI init"

        hist_json = state.execute_command("list")
        assert '"version": 1' in hist_json


class TestAIAgentHarnessIntegration:
    def test_agent_harness_update_and_rollback(self, tmp_path):
        agent = AIAgent(
            api_key="mock-key",
            base_url="http://localhost:8080/v1",
            model="test-model",
            quiet_mode=True,
            session_id="agent_harness_sess",
        )
        init_instr = agent.get_harness_instructions()

        # Update instructions mid-task
        v2 = agent.update_harness_instructions(
            "New operating rules: write Python 3.11 typed code only",
            reason="Adopt strict typing",
        )
        assert v2 is not None
        assert v2.version == 2
        assert (
            agent.get_harness_instructions()
            == "New operating rules: write Python 3.11 typed code only"
        )

        # Rollback bad instruction edit
        v3 = agent.rollback_harness_instructions(
            reason="Revert strict typing for legacy project"
        )
        assert v3 is not None
        assert v3.version == 3
        assert agent.get_harness_instructions() == init_instr
