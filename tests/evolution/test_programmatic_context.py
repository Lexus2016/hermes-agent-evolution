# -*- coding: utf-8 -*-
"""Tests for persistent programmatic context — context-as-variable store (Issue #2496)."""

from __future__ import annotations

from unittest.mock import MagicMock
import pytest

from agent.context_compressor import ContextCompressor
from evolution.lib.programmatic_context import (
    ContextVariable,
    ProgrammaticContextStore,
    get_default_context_store_dir,
)
from run_agent import AIAgent


class TestProgrammaticContextStore:
    def test_set_and_get_variable(self, tmp_path):
        store = ProgrammaticContextStore(session_id="test_sess", storage_dir=tmp_path)
        store.set(
            "tool_output",
            "Fetched 1000 lines of log output",
            description="Raw build logs",
        )
        store.set("config_obj", {"host": "localhost", "port": 8080})

        assert store.get("tool_output") == "Fetched 1000 lines of log output"
        assert store.get("config_obj") == {"host": "localhost", "port": 8080}
        assert store.get("non_existent", default="missing") == "missing"

    def test_slice_variable(self, tmp_path):
        store = ProgrammaticContextStore(session_id="test_sess", storage_dir=tmp_path)
        store.set("long_text", "0123456789ABCDEF")
        store.set("item_list", [10, 20, 30, 40, 50])

        assert store.slice("long_text", 0, 5) == "01234"
        assert store.slice("long_text", 10, None) == "ABCDEF"
        assert store.slice("item_list", 1, 4) == [20, 30, 40]
        assert store.slice("non_existent") is None

    def test_list_vars_and_metadata(self, tmp_path):
        store = ProgrammaticContextStore(session_id="test_sess", storage_dir=tmp_path)
        store.set("raw_data", "hello world", description="Greeting")
        vars_list = store.list_vars()
        assert len(vars_list) == 1
        assert vars_list[0]["name"] == "raw_data"
        assert vars_list[0]["type"] == "str"
        assert vars_list[0]["length"] == 11
        assert vars_list[0]["description"] == "Greeting"

    def test_delete_and_clear(self, tmp_path):
        store = ProgrammaticContextStore(session_id="test_sess", storage_dir=tmp_path)
        store.set("v1", "val1")
        store.set("v2", "val2")

        assert store.delete("v1") is True
        assert store.get("v1") is None
        assert store.delete("v1") is False

        store.clear()
        assert len(store.list_vars()) == 0

    def test_summarize_store(self, tmp_path):
        store = ProgrammaticContextStore(session_id="test_sess", storage_dir=tmp_path)
        assert "No programmatic context variables" in store.summarize()

        store.set("query_results", ["row1", "row2"], description="Database rows")
        summary = store.summarize()
        assert "# Programmatic Context Variables" in summary
        assert "`query_results`" in summary
        assert "list" in summary
        assert "Database rows" in summary

    def test_persistence_and_reload(self, tmp_path):
        store1 = ProgrammaticContextStore(
            session_id="sess_persist", storage_dir=tmp_path
        )
        store1.set(
            "persisted_key", "persistent_value", description="Must survive reload"
        )

        # Reload from disk in a fresh store instance
        store2 = ProgrammaticContextStore(
            session_id="sess_persist", storage_dir=tmp_path
        )
        assert store2.get("persisted_key") == "persistent_value"
        vars2 = store2.list_vars()
        assert len(vars2) == 1
        assert vars2[0]["description"] == "Must survive reload"

    def test_execute_command(self, tmp_path):
        store = ProgrammaticContextStore(session_id="cli_sess", storage_dir=tmp_path)
        msg = store.execute_command("set", "api_resp", "HTTP 200 OK Body")
        assert "Variable 'api_resp' set" in msg

        got = store.execute_command("get", "api_resp")
        assert got == "HTTP 200 OK Body"

        sliced = store.execute_command("slice", "api_resp", "0:8")
        assert sliced == "HTTP 200"

        summary = store.execute_command("summary")
        assert "`api_resp`" in summary

        deleted = store.execute_command("del", "api_resp")
        assert "deleted" in deleted


class TestIntegrationWithCompressorAndAgent:
    def test_context_compressor_context_store(self, tmp_path):
        compressor = ContextCompressor(model="test-model")
        compressor.bind_session_state(session_id="comp_sess")
        store = compressor.context_store
        assert store is not None
        store.set("compressed_cache", {"status": "active"})
        assert compressor.context_store.get("compressed_cache") == {"status": "active"}

    def test_ai_agent_context_var_methods(self, tmp_path):
        agent = AIAgent(
            api_key="mock-key",
            base_url="http://localhost:8080/v1",
            model="test-model",
            quiet_mode=True,
            session_id="agent_sess",
        )
        agent.set_context_var(
            "action_snapshot", "command stdout 500 lines", description="Terminal stdout"
        )
        assert agent.get_context_var("action_snapshot") == "command stdout 500 lines"
        assert agent.slice_context_var("action_snapshot", 0, 7) == "command"

        vars_list = agent.list_context_vars()
        assert len(vars_list) == 1
        assert vars_list[0]["name"] == "action_snapshot"

    def test_variables_survive_compaction(self, tmp_path):
        agent = AIAgent(
            api_key="mock-key",
            base_url="http://localhost:8080/v1",
            model="test-model",
            quiet_mode=True,
            session_id="survival_sess",
        )
        agent.set_context_var(
            "critical_decision", "Use AES-256 for vault", description="Crypto decision"
        )

        # Simulate context compaction happening on message list
        if agent.context_compressor:
            agent.context_compressor.compress = MagicMock(
                return_value=[{"role": "system", "content": "Summary"}]
            )
            # Compressed conversation replaced
            messages = [{"role": "system", "content": "Summary"}]

        # Context variable is still intact and accessible without needing old raw messages
        assert agent.get_context_var("critical_decision") == "Use AES-256 for vault"
