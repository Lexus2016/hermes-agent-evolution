"""Tests for the shared read-only memory tier (issue #1877)."""

import json
import os
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from scripts.evolution_shared_memory import (
    _query_global_scope,
    _read_cache,
    _write_cache,
    retrieve_shared_experiences,
    run_shared_memory_sync,
)


def test_query_global_scope_empty(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    assert _query_global_scope() == []


def test_query_global_scope_filesystem(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    gdir = tmp_path / "tqmemory" / "global" / "notes"
    gdir.mkdir(parents=True)
    (gdir / "n1.json").write_text(
        json.dumps({
            "id": "note-1",
            "title": "Lesson",
            "content": "JS pages fail",
            "tags": ["evolution", "lesson"],
            "source_refs": [],
            "note_kind": "lesson",
        })
    )
    (gdir / "n2.json").write_text(
        json.dumps({
            "id": "note-2",
            "title": "Unrelated",
            "content": "other",
            "tags": ["random"],
            "source_refs": [],
        })
    )
    result = _query_global_scope(query="evolution")
    assert len(result) == 1
    assert result[0]["id"] == "note-1"
    assert "evolution" in result[0]["tags"]


def test_read_cache_empty(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    assert _read_cache() == []


def test_write_and_read_cache(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    notes = [{"id": "n1", "title": "test", "content": "c", "tags": ["evolution"]}]
    _write_cache(notes)
    cached = _read_cache()
    assert len(cached) == 1
    assert cached[0]["id"] == "n1"


def test_retrieve_shared_experiences_uses_cache(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    # No global notes dir → query returns [], falls back to cache
    evo = tmp_path / "evolution"
    evo.mkdir()
    _write_cache([{"id": "cached-note", "title": "cached", "content": "c", "tags": []}])
    result = retrieve_shared_experiences()
    assert len(result) == 1
    assert result[0]["id"] == "cached-note"


def test_retrieve_updates_cache_on_live_query(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    gdir = tmp_path / "tqmemory" / "global" / "notes"
    gdir.mkdir(parents=True)
    (gdir / "n1.json").write_text(
        json.dumps({
            "id": "live-note",
            "title": "live",
            "content": "c",
            "tags": ["evolution"],
            "source_refs": [],
            "note_kind": "pattern",
        })
    )
    result = retrieve_shared_experiences()
    assert len(result) == 1
    assert result[0]["id"] == "live-note"
    # Cache should now contain the live note
    cached = _read_cache()
    assert len(cached) == 1
    assert cached[0]["id"] == "live-note"


def test_run_shared_memory_sync(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    gdir = tmp_path / "tqmemory" / "global" / "notes"
    gdir.mkdir(parents=True)
    (gdir / "n1.json").write_text(
        json.dumps({
            "id": "n1",
            "title": "t",
            "content": "c",
            "tags": ["evolution"],
            "source_refs": [],
        })
    )
    result = run_shared_memory_sync()
    assert result["status"] == "ok"
    assert result["notes_synced"] == 1
    assert result["cached"] is True


def test_run_shared_memory_sync_empty(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    result = run_shared_memory_sync()
    assert result["status"] == "ok"
    assert result["notes_synced"] == 0
    assert result["cached"] is False


def test_cache_file_created(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    gdir = tmp_path / "tqmemory" / "global" / "notes"
    gdir.mkdir(parents=True)
    (gdir / "n1.json").write_text(
        json.dumps({
            "id": "n1",
            "title": "t",
            "content": "c",
            "tags": ["evolution"],
            "source_refs": [],
        })
    )
    run_shared_memory_sync()
    cache = tmp_path / "evolution" / "shared-memory-cache.json"
    assert cache.exists()
    data = json.loads(cache.read_text())
    assert "notes" in data
    assert "updated_at" in data
    assert data["note_count"] == 1


def test_non_evolution_tags_excluded(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    gdir = tmp_path / "tqmemory" / "global" / "notes"
    gdir.mkdir(parents=True)
    (gdir / "n1.json").write_text(
        json.dumps({
            "id": "n1",
            "title": "nope",
            "content": "c",
            "tags": ["cooking", "recipes"],
            "source_refs": [],
        })
    )
    result = _query_global_scope()
    assert result == []  # non-evolution notes excluded
