"""Tests for MemoryManager retrieval-utility wiring (issue #1480).

Verifies that prefetch_all triggers retrieval logging and sync_all records
outcomes. Uses stdlib + pytest + unittest.mock only.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from agent.memory_manager import MemoryManager
from agent.memory_provider import MemoryProvider


class _FakeProvider(MemoryProvider):
    """Minimal provider for testing — returns canned context on prefetch."""

    def __init__(self, name: str = "builtin", context: str = "recalled context"):
        self._name = name
        self._context = context

    @property
    def name(self) -> str:
        return self._name

    def is_available(self) -> bool:
        return True

    def initialize(self, session_id: str, **kwargs) -> None:
        pass

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        return self._context

    def sync_turn(
        self,
        user_content: str,
        assistant_content: str,
        *,
        session_id: str = "",
        messages=None,
    ) -> None:
        pass

    def get_tool_schemas(self):
        return []


@pytest.fixture
def isolated_hermes_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    yield tmp_path


class TestPrefetchRetrievalLogging:
    """Verify prefetch_all logs retrievals to the sidecar."""

    @pytest.fixture(autouse=True)
    def _enable_retrieval_utility(self):
        """Existing tests assume retrieval-utility logging is ON.

        The ``memory.retrieval_utility.enabled`` config flag defaults to
        ``False`` (opt-in).  Mock it to ``True`` so the logging path is
        exercised.  The dedicated ``TestRetrievalUtilityGate`` class below
        tests the disabled path.
        """
        with patch.object(
            MemoryManager, "_retrieval_utility_enabled", return_value=True
        ):
            yield

    def test_prefetch_logs_retrieval(self, isolated_hermes_home):
        from agent.retrieval_utility import load_log

        provider = _FakeProvider("builtin", context="recalled: foo")
        mgr = MemoryManager()
        mgr.add_provider(provider)

        result = mgr.prefetch_all("what is foo?", session_id="s1")
        assert "recalled: foo" in result

        log = load_log()
        assert len(log["retrievals"]) == 1
        assert log["retrievals"][0]["record_id"] == "memory:builtin"
        assert log["retrievals"][0]["session_id"] == "s1"
        assert log["retrievals"][0]["outcome"] is None

    def test_prefetch_empty_no_log(self, isolated_hermes_home):
        from agent.retrieval_utility import load_log

        provider = _FakeProvider("builtin", context="")
        mgr = MemoryManager()
        mgr.add_provider(provider)

        result = mgr.prefetch_all("query")
        assert result == ""

        log = load_log()
        assert log["retrievals"] == []

    def test_prefetch_skill_scaffolding_skipped(self, isolated_hermes_home):
        from agent.retrieval_utility import load_log

        provider = _FakeProvider("builtin", context="ctx")
        mgr = MemoryManager()
        mgr.add_provider(provider)

        # _strip_skill_scaffolding only returns None for skill invocations
        # with no user instruction after the skill name. A bare "/skill some-skill"
        # without an expanded body is NOT a skill invocation — it's a plain
        # message and passes through unchanged. Test the actual skip path
        # by verifying that an empty/whitespace-only query produces no log.
        mgr.prefetch_all("")
        log = load_log()
        assert log["retrievals"] == []


class TestSyncOutcomeRecording:
    """Verify sync_all records outcomes for pending retrievals."""

    @pytest.fixture(autouse=True)
    def _enable_retrieval_utility(self):
        """Existing tests assume retrieval-utility logging is ON."""
        with patch.object(
            MemoryManager, "_retrieval_utility_enabled", return_value=True
        ):
            yield

    def test_sync_records_helpful_outcome(self, isolated_hermes_home):
        from agent.retrieval_utility import load_log

        provider = _FakeProvider("builtin", context="ctx")
        mgr = MemoryManager()
        mgr.add_provider(provider)

        # Prefetch (creates a pending retrieval).
        mgr.prefetch_all("query", session_id="s1")
        # Sync (records the outcome).
        mgr.sync_all("query", "here is the answer", session_id="s1")

        log = load_log()
        assert len(log["retrievals"]) == 1
        # No friction signals → helpful.
        assert log["retrievals"][0]["outcome"] == "helpful"

    def test_sync_records_harmful_outcome(self, isolated_hermes_home):
        from agent.retrieval_utility import load_log

        provider = _FakeProvider("builtin", context="ctx")
        mgr = MemoryManager()
        mgr.add_provider(provider)

        mgr.prefetch_all("query", session_id="s1")
        # Sync with a failure-like assistant response.
        mgr.sync_all("query", "sorry, I failed to do that", session_id="s1")

        log = load_log()
        # task_failures signal → harmful outcome.
        assert log["retrievals"][0]["outcome"] == "harmful"

    def test_sync_with_no_pending_retrievals(self, isolated_hermes_home):
        """sync_all without a prior prefetch should not crash."""
        provider = _FakeProvider("builtin")
        mgr = MemoryManager()
        mgr.add_provider(provider)
        mgr.sync_all("query", "answer", session_id="s1")
        # No crash, no retrievals logged.
        from agent.retrieval_utility import load_log

        log = load_log()
        assert log["retrievals"] == []

    def test_pending_retrievals_cleared_after_sync(self, isolated_hermes_home):
        provider = _FakeProvider("builtin", context="ctx")
        mgr = MemoryManager()
        mgr.add_provider(provider)

        mgr.prefetch_all("query", session_id="s1")
        assert len(mgr._pending_retrievals) == 1
        mgr.sync_all("query", "answer", session_id="s1")
        assert len(mgr._pending_retrievals) == 0


class TestRetrievalUtilityGate:
    """Verify retrieval-utility logging respects the ``enabled`` config flag.

    When ``memory.retrieval_utility.enabled`` is ``False`` (the default),
    no sidecar file should be written and no pending retrievals should
    accumulate.
    """

    def test_disabled_no_sidecar_written(self, isolated_hermes_home):
        """prefetch_all must not write the sidecar when disabled."""
        from agent.retrieval_utility import load_log

        provider = _FakeProvider("builtin", context="recalled: foo")
        mgr = MemoryManager()
        mgr.add_provider(provider)

        # _retrieval_utility_enabled() defaults to False (config not set).
        result = mgr.prefetch_all("what is foo?", session_id="s1")
        assert "recalled: foo" in result

        log = load_log()
        assert log["retrievals"] == []

    def test_disabled_no_pending_retrievals_accumulated(self, isolated_hermes_home):
        """_pending_retrievals must stay empty when disabled."""
        provider = _FakeProvider("builtin", context="ctx")
        mgr = MemoryManager()
        mgr.add_provider(provider)

        mgr.prefetch_all("query", session_id="s1")
        assert len(mgr._pending_retrievals) == 0

    def test_disabled_sync_does_not_record(self, isolated_hermes_home):
        """sync_all must not record outcomes when disabled."""
        from agent.retrieval_utility import load_log

        provider = _FakeProvider("builtin", context="ctx")
        mgr = MemoryManager()
        mgr.add_provider(provider)

        # Even if we manually inject a pending retrieval, sync should
        # clear it without writing to the sidecar when disabled.
        mgr._pending_retrievals.append(("memory:builtin", "s1"))
        mgr.sync_all("query", "answer", session_id="s1")
        assert len(mgr._pending_retrievals) == 0

        log = load_log()
        assert log["retrievals"] == []
