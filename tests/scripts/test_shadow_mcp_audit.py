"""Tests for scripts/shadow_mcp_audit.py (issue #90)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from shadow_mcp_audit import (
    main,
    read_contacts,
)


def _write_log(path: Path, records: list[dict]) -> None:
    path.write_text(
        "\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8"
    )


def test_read_contacts_keeps_final_aggregate(tmp_path):
    log = tmp_path / "shadow.jsonl"
    _write_log(
        log,
        [
            {"server": "srv", "endpoint": "https://a.com/x", "count": 1,
             "last_verdict": "allow", "last_seen": "2026-01-01T00:00:00Z"},
            {"server": "srv", "endpoint": "https://a.com/x", "count": 2,
             "last_verdict": "alert", "last_seen": "2026-01-01T00:00:01Z"},
            {"server": "srv", "endpoint": "https://b.com/x", "count": 1,
             "last_verdict": "deny", "last_seen": "2026-01-01T00:00:02Z"},
        ],
    )
    contacts = read_contacts(log)
    assert len(contacts) == 2
    by_endpoint = {c["endpoint"]: c for c in contacts}
    assert by_endpoint["https://a.com/x"]["count"] == 2
    assert by_endpoint["https://a.com/x"]["last_verdict"] == "alert"
    assert by_endpoint["https://b.com/x"]["last_verdict"] == "deny"


def test_read_contacts_missing_or_corrupt(tmp_path):
    assert read_contacts(tmp_path / "nope.jsonl") == []
    log = tmp_path / "shadow.jsonl"
    _write_log(log, [{"bad": "not-a-record"}])
    contacts = read_contacts(log)
    assert len(contacts) == 1
    # Records without server/endpoint keys are still surfaced; the CLI reads
    # them defensively via .get().
    assert contacts[0].get("server", "") == ""
    assert contacts[0]["bad"] == "not-a-record"


def test_main_json_reports_unapproved_and_exit_code(tmp_path, capsys):
    log = tmp_path / "shadow.jsonl"
    _write_log(
        log,
        [
            {"server": "srv", "endpoint": "https://ok.com/x", "count": 1,
             "last_verdict": "allow", "last_seen": "2026-01-01T00:00:00Z"},
            {"server": "srv", "endpoint": "https://bad.com/x", "count": 1,
             "last_verdict": "alert", "last_seen": "2026-01-01T00:00:01Z"},
        ],
    )
    rc = main(["--log", str(log), "--json"])
    assert rc == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["unapproved"][0]["endpoint"] == "https://bad.com/x"


def test_main_clean_returns_zero(tmp_path, capsys):
    log = tmp_path / "shadow.jsonl"
    _write_log(
        log,
        [
            {"server": "srv", "endpoint": "https://ok.com/x", "count": 1,
             "last_verdict": "allow", "last_seen": "2026-01-01T00:00:00Z"},
        ],
    )
    assert main(["--log", str(log)]) == 0


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
