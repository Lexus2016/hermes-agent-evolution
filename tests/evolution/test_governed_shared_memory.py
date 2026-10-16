"""Tests for governed shared memory with scope, provenance, supersession, and redistribution (Issue #2488, Slice B, MemClaw)."""

from __future__ import annotations

import pytest

from evolution.lib.governed_shared_memory import (
    GovernedMemoryRecord,
    GovernedSharedMemory,
    MemoryProvenance,
    MemoryScope,
    get_global_governed_memory,
)
from run_agent import AIAgent


class TestGovernedSharedMemory:
    def test_write_and_read_record(self):
        mem = GovernedSharedMemory()
        rec = mem.write(
            key="config_cache",
            value={"rate_limit": 60},
            author_id="worker_alpha",
            scope=MemoryScope.TASK.value,
            source_tool="read_file",
            sources=["file:///config.json"],
            confidence=0.95,
        )
        assert isinstance(rec, GovernedMemoryRecord)
        assert rec.key == "config_cache"
        assert rec.value == {"rate_limit": 60}
        assert rec.provenance.author_subagent_id == "worker_alpha"
        assert rec.provenance.source_tool == "read_file"
        assert rec.is_active is True

        fetched = mem.read("config_cache")
        assert fetched is not None
        assert fetched.value == {"rate_limit": 60}

    def test_supersession_logic(self):
        mem = GovernedSharedMemory()
        # Original memory
        r1 = mem.write("doc_v1", "Initial summary", author_id="agent_1")
        assert r1.is_active is True

        # Superseding memory
        r2 = mem.write(
            key="doc_v2",
            value="Refined summary",
            author_id="agent_2",
            supersedes_key="doc_v1",
        )

        assert r2.is_active is True
        assert r2.supersedes_key == "doc_v1"

        # Old record must be inactive and linked to r2
        old = mem.read("doc_v1", active_only=False)
        assert old is not None
        assert old.is_active is False
        assert old.superseded_by == "doc_v2"

        # Active-only read returns None for superseded
        assert mem.read("doc_v1", active_only=True) is None

    def test_provenance_chain_tracing(self):
        mem = GovernedSharedMemory()
        mem.write("step_1", "draft 1", author_id="a1")
        mem.write("step_2", "draft 2", author_id="a2", supersedes_key="step_1")
        mem.write("step_3", "draft 3", author_id="a3", supersedes_key="step_2")

        chain = mem.get_provenance_chain("step_3")
        assert len(chain) == 3
        assert [r.key for r in chain] == ["step_3", "step_2", "step_1"]
        assert [r.provenance.author_subagent_id for r in chain] == ["a3", "a2", "a1"]

    def test_scope_filtering(self):
        mem = GovernedSharedMemory()
        mem.write("k_task", 1, author_id="w1", scope=MemoryScope.TASK.value)
        mem.write("k_global", 2, author_id="w1", scope=MemoryScope.GLOBAL.value)
        mem.write("k_local", 3, author_id="w2", scope=MemoryScope.LOCAL.value)

        task_recs = mem.list_by_scope(MemoryScope.TASK.value)
        assert len(task_recs) == 1
        assert task_recs[0].key == "k_task"

        global_recs = mem.list_by_scope(MemoryScope.GLOBAL.value)
        assert len(global_recs) == 1
        assert global_recs[0].key == "k_global"

    def test_redistribute_subagent_memory(self):
        mem = GovernedSharedMemory()
        mem.write("task_a", "state A", author_id="crashed_worker")
        mem.write("task_b", "state B", author_id="crashed_worker")
        mem.write("task_other", "state O", author_id="other_worker")

        # Redistribute from crashed_worker to replacement_worker
        rehomed = mem.redistribute(
            superseded_subagent_id="crashed_worker",
            successor_subagent_id="replacement_worker",
        )
        assert rehomed == 2

        # Active records now authored by replacement_worker with audit trail in sources
        rec_a = mem.read("task_a")
        assert rec_a is not None
        assert rec_a.provenance.author_subagent_id == "replacement_worker"
        assert "rehomed_from:crashed_worker" in rec_a.provenance.sources

        author_recs = mem.list_by_author("replacement_worker")
        assert len(author_recs) == 2

    def test_reader_class_authorization(self):
        mem = GovernedSharedMemory()
        mem.write(
            "orchestrator_secret",
            "s1",
            author_id="boss",
            reader_class="orchestrator",
        )

        # Matching class can read
        assert mem.read("orchestrator_secret", reader_class="orchestrator") is not None
        # Wrong class refused
        assert mem.read("orchestrator_secret", reader_class="stage") is None
        # No class refused (fail-closed)
        assert mem.read("orchestrator_secret") is None

    def test_legacy_records_unrestricted(self):
        mem = GovernedSharedMemory()
        mem.write("plain", "v", author_id="a1")
        assert mem.read("plain") is not None
        assert mem.read("plain", reader_class="orchestrator") is not None
        assert mem.read("plain", reader_class="stage") is not None

    def test_restriction_propagates_through_supersession(self):
        mem = GovernedSharedMemory()
        mem.write("doc_v1", "initial", author_id="boss", reader_class="orchestrator")
        # Superseding WITHOUT declaring a class must not launder the restriction
        v2 = mem.write(
            "doc_v2", "summary", author_id="summarizer", supersedes_key="doc_v1"
        )
        assert v2.reader_class == "orchestrator"
        assert mem.read("doc_v2", reader_class="stage") is None
        assert mem.read("doc_v2", reader_class="orchestrator") is not None
        # Explicit class cannot widen a restricted source
        v3 = mem.write(
            "doc_v3",
            "re",
            author_id="x",
            supersedes_key="doc_v2",
            reader_class="stage",
        )
        assert v3.reader_class == "orchestrator"

    def test_list_paths_respect_reader_class(self):
        mem = GovernedSharedMemory()
        mem.write(
            "k1",
            1,
            author_id="w1",
            scope=MemoryScope.TASK.value,
            reader_class="orchestrator",
        )
        mem.write("k2", 2, author_id="w1", scope=MemoryScope.TASK.value)
        # Restricted record hidden from unauthenticated listing
        assert len(mem.list_by_scope(MemoryScope.TASK.value)) == 1
        assert (
            len(mem.list_by_scope(MemoryScope.TASK.value, reader_class="orchestrator"))
            == 2
        )
        assert len(mem.list_by_author("w1")) == 1
        assert len(mem.list_by_author("w1", reader_class="orchestrator")) == 2

    def test_redistribute_preserves_reader_class(self):
        mem = GovernedSharedMemory()
        mem.write("task_a", "state", author_id="old_worker", reader_class="stage")
        mem.redistribute("old_worker", "new_worker")
        rec = mem.read("task_a", reader_class="stage")
        assert rec is not None
        assert rec.reader_class == "stage"
        assert mem.read("task_a", reader_class="orchestrator") is None


class TestAIAgentGovernedMemoryIntegration:
    def test_agent_governed_memory_methods(self):
        agent = AIAgent(
            api_key="mock-key",
            base_url="http://localhost:8080/v1",
            model="test-model",
            quiet_mode=True,
            session_id="test_agent_gov_sess",
        )

        rec = agent.write_governed_memory(
            key="agent_finding",
            value="Optimization possible",
            scope="task",
        )
        assert rec.key == "agent_finding"
        assert rec.value == "Optimization possible"

        fetched = agent.read_governed_memory("agent_finding")
        assert fetched is not None
        assert fetched.value == "Optimization possible"

        # Redistribute test via agent method
        agent.write_governed_memory(
            key="sub_work",
            value="sub output",
            author_id="sub_old",
        )
        count = agent.redistribute_subagent_memory("sub_old", "sub_new")
        assert count == 1
        assert (
            agent.read_governed_memory("sub_work").provenance.author_subagent_id
            == "sub_new"
        )

    def test_agent_governed_memory_reader_class(self):
        agent = AIAgent(
            api_key="mock-key",
            base_url="http://localhost:8080/v1",
            model="test-model",
            quiet_mode=True,
            session_id="test_agent_gov_sess_rc",
        )

        agent.write_governed_memory(
            key="restricted_finding",
            value="secret",
            scope="task",
            reader_class="orchestrator",
        )
        assert (
            agent.read_governed_memory(
                "restricted_finding", reader_class="orchestrator"
            )
            is not None
        )
        assert (
            agent.read_governed_memory("restricted_finding", reader_class="stage")
            is None
        )
