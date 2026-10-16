"""Unattended memory review mutation admission regressions."""
import json

import pytest

from tools.memory_tool import MemoryStore, memory_tool
from tools.skill_provenance import reset_current_write_origin, set_current_write_origin


@pytest.fixture()
def store(tmp_path, monkeypatch):
    """Create a MemoryStore with temp storage."""
    monkeypatch.setattr("tools.memory_tool.get_memory_dir", lambda: tmp_path)
    s = MemoryStore(memory_char_limit=500, user_char_limit=300)
    s.load_from_disk()
    return s



class TestBackgroundReviewDeleteGate:
    """An unattended background-review fork may append, never delete: the near-limit
    'consolidate now' hint is otherwise an instruction to decide what to forget,
    executed with no human in the loop. Denied ops are staged as pending proposals
    (surfaced via /memory pending) instead of silently dropped — the fork's own review
    summary is never published back."""

    def test_remove_staged_not_applied(self, store, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        store.add("memory", "never create records without permission")
        token = set_current_write_origin("background_review")
        try:
            result = json.loads(memory_tool(action="remove", old_text="without permission", store=store))
        finally:
            reset_current_write_origin(token)
        assert result["success"] is True
        assert result["staged"] is True
        assert result["proposal_staged"] is True
        assert result["pending_id"]
        # Fail-closed: the standing rule is still on disk.
        assert "never create records without permission" in store._entries_for("memory")
        # The proposal itself landed in the pending store for the user to approve or discard.
        from tools.write_approval import MEMORY, get_pending
        record = get_pending(MEMORY, result["pending_id"])
        assert record["payload"]["action"] == "remove"
        assert record["origin"] == "background_review"

    def test_patch_staged_in_background_review(self, store, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        store.add("memory", "entry the fork must not rewrite")
        token = set_current_write_origin("background_review")
        try:
            result = json.loads(memory_tool(
                action="patch", old_text="entry the fork", content="rewritten by fork", store=store))
        finally:
            reset_current_write_origin(token)
        assert result["staged"] is True
        assert result["proposal_staged"] is True
        # Fail-closed: the original entry is untouched.
        assert "entry the fork must not rewrite" in store._entries_for("memory")

    def test_batch_containing_remove_staged_whole_batch(self, store, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        store.add("memory", "rule one")
        token = set_current_write_origin("background_review")
        try:
            result = json.loads(memory_tool(operations=[
                {"action": "remove", "old_text": "rule one"},
                {"action": "add", "content": "fork consolidation"},
            ], store=store))
        finally:
            reset_current_write_origin(token)
        assert result["staged"] is True
        # Atomic: the batch is only a proposal — its add must not land either.
        assert "fork consolidation" not in store._entries_for("memory")

    def test_add_still_allowed_in_background_review(self, store):
        token = set_current_write_origin("background_review")
        try:
            result = json.loads(memory_tool(action="add", content="a fact worth keeping", store=store))
        finally:
            reset_current_write_origin(token)
        assert result["success"] is True
        assert "a fact worth keeping" in store._entries_for("memory")

    def test_foreground_remove_unaffected(self, store):
        store.add("memory", "entry a supervised turn may remove")
        result = json.loads(memory_tool(action="remove", old_text="supervised turn", store=store))
        assert result["success"] is True
        assert "entry a supervised turn may remove" not in store._entries_for("memory")

    def test_attended_review_keeps_full_operation_set(self, store):
        # A user-requested /refine fork keeps the background_review origin (skill guards still
        # apply) but is attended, so patch/remove keep working on that supervised surface.
        from tools.skill_provenance import reset_review_attended, set_review_attended
        store.add("memory", "entry an explicit refine may rewrite")
        token = set_current_write_origin("background_review")
        att = set_review_attended(True)
        try:
            result = json.loads(memory_tool(
                action="patch", old_text="entry an explicit refine may rewrite", content="rewritten by refine",
                store=store))
        finally:
            reset_review_attended(att)
            reset_current_write_origin(token)
        assert result["success"] is True
        assert "rewritten by refine" in store._entries_for("memory")
