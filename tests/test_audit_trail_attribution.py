"""Tests for per-call attribution stamping in the audit trail (evo-2026-08-26-03)."""

from __future__ import annotations

import json

from agent import audit_trail


def _payload(entry: dict) -> dict:
    return (
        json.loads(entry["payload"])
        if isinstance(entry["payload"], str)
        else entry["payload"]
    )


def test_no_attribution_by_default(tmp_path, monkeypatch):
    monkeypatch.delenv("HERMES-SUBAGENT-ATTRIBUTION", raising=False)
    path = tmp_path / "trail.jsonl"
    entry = audit_trail.append({"event": "tool_call"}, path=path)
    record = _payload(entry)
    assert "attribution" not in record
    assert "attribution" not in record.get("metadata", {})


def test_attribution_stamped_from_env(tmp_path, monkeypatch):
    marker = (
        "HERMES-SUBAGENT-ATTRIBUTION subagent_id=sa-9 parent=root "
        "task_index=0 spawned_at=2026-08-26T00:00:00+00:00"
    )
    monkeypatch.setenv("HERMES-SUBAGENT-ATTRIBUTION", marker)
    path = tmp_path / "trail.jsonl"
    entry = audit_trail.append({"event": "tool_call"}, path=path)
    # In-memory return value carries the stamp...
    record = _payload(entry)
    assert record["metadata"]["attribution"] == marker
    assert record["attribution"] == marker
    # ...and so does the persisted chain entry.
    persisted = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
    persisted_record = _payload(persisted)
    assert persisted_record["attribution"] == marker


def test_existing_metadata_preserved(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES-SUBAGENT-ATTRIBUTION", "HERMES-SUBAGENT-ATTRIBUTION x=1")
    path = tmp_path / "trail.jsonl"
    entry = audit_trail.append(
        {"event": "tool_call", "metadata": {"tool": "terminal"}}, path=path
    )
    metadata = _payload(entry)["metadata"]
    assert metadata["tool"] == "terminal"
    assert metadata["attribution"] == "HERMES-SUBAGENT-ATTRIBUTION x=1"
