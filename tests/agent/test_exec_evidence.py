"""Tests for independent execution-evidence capture (issue #1716)."""

import json

import pytest

from agent import exec_evidence


@pytest.fixture(autouse=True)
def isolate_hermes_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))


def test_record_creates_entry(tmp_path):
    entry = exec_evidence.record_evidence("terminal", {"command": "ls"})
    assert entry["tool"] == "terminal"
    assert "ls" in entry["args"]
    assert exec_evidence.evidence_count() == 1


def test_verify_claim_backed_by_evidence(tmp_path):
    exec_evidence.record_evidence("read_file", {"path": "/etc/passwd"})
    exec_evidence.record_evidence("terminal", {"command": "whoami"})
    assert exec_evidence.verify_claim("terminal")
    assert exec_evidence.verify_claim("read_file")
    assert not exec_evidence.verify_claim("write_file")


def test_verify_claim_false_for_empty_store(tmp_path):
    assert not exec_evidence.verify_claim("anything")


def test_args_truncation(tmp_path):
    big = {"data": "x" * 500}
    entry = exec_evidence.record_evidence("search", big)
    assert len(entry["args"]) <= 200


def test_corrupt_line_skipped(tmp_path):
    exec_evidence.record_evidence("terminal", {"command": "ls"})
    path = exec_evidence.evidence_path()
    with open(path, "a", encoding="utf-8") as fh:
        fh.write("NOT JSON\n")
    assert exec_evidence.evidence_count() == 1
