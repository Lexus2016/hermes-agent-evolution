"""Tests for the tamper-evident audit trail (issue #1719)."""

import json

import pytest

from agent import audit_trail


@pytest.fixture(autouse=True)
def isolate_hermes_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))


def test_append_creates_chained_entries(tmp_path):
    log = tmp_path / "audit.jsonl"
    first = audit_trail.append({"action": "read"}, path=log)
    second = audit_trail.append({"action": "write"}, path=log)

    assert first["prev_hash"] == audit_trail._GENESIS
    assert second["prev_hash"] == first["hash"]
    assert first["hash"] != second["hash"]
    assert audit_trail.verify(log) == (True, 2)


def test_verify_detects_tamper(tmp_path):
    log = tmp_path / "audit.jsonl"
    audit_trail.append({"action": "read"}, path=log)

    first = json.loads(log.read_text().splitlines()[0])
    first["payload"] = json.dumps({"action": "EVIL"})
    log.write_text(json.dumps(first, sort_keys=True) + "\n")

    assert audit_trail.verify(log)[0] is False


def test_prune_drops_old_and_reanchors(tmp_path):
    log = tmp_path / "audit.jsonl"
    prev = audit_trail._GENESIS
    lines = []
    for ts, action in ((1000000, "old"), (99999999999, "new")):
        payload = json.dumps({"ts": ts, "action": action}, sort_keys=True)
        entry = {
            "ts": ts,
            "prev_hash": prev,
            "payload": payload,
            "hash": audit_trail._hash(prev, payload),
        }
        lines.append(json.dumps(entry, sort_keys=True))
        prev = entry["hash"]
    log.write_text("\n".join(lines) + "\n")

    assert audit_trail.prune(now=99999999999, path=log) == 1
    kept = json.loads(log.read_text().splitlines()[0])
    assert kept["prev_hash"] == audit_trail._GENESIS and audit_trail.verify(log) == (
        True,
        1,
    )


def test_retention_days_default_and_config(tmp_path, monkeypatch):
    assert audit_trail.retention_days() == audit_trail.DEFAULT_RETENTION_DAYS

    (tmp_path / ".hermes" / "config.yaml").write_text(
        "security:\n  audit:\n    retention_days: 30\n"
    )
    import hermes_cli.config as cfg

    cfg._LOAD_CONFIG_CACHE.clear()
    cfg._RAW_CONFIG_CACHE.clear()
    assert audit_trail.retention_days() == 30
