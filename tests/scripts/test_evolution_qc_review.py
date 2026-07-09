"""Tests for evolution_qc_review.py — agentic QC review loop (#796)."""

import io
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
from evolution_qc_review import build_qc_review_task, parse_qc_report, main  # noqa: E402  # fmt: skip

_j = json.dumps


def test_build_task():
    task = build_qc_review_task("Implemented X", ["a.py", "b.py"], issue_number=796)
    assert task["role"] == "leaf" and "Implemented X" in task["goal"]
    assert (
        "a.py" in task["goal"]
        and "#796" in task["context"]
        and "file" in task["toolsets"]
    )


def test_build_no_files():
    assert "no files listed" in build_qc_review_task("s", [])["goal"]


def test_parse_pass():
    r = parse_qc_report(_j({"verdict": "pass", "has_blocking": False, "issues": [], "recommendations": ["x"]}))  # fmt: skip
    assert r["ok"] and r["verdict"] == "pass" and not r["has_blocking"]


def test_parse_fail_blocking():
    r = parse_qc_report(_j({"verdict": "fail", "has_blocking": True, "issues": [{"severity": "high"}]}))  # fmt: skip
    assert not r["ok"] and r["has_blocking"]


def test_parse_pass_blocking_not_ok():
    assert not parse_qc_report(_j({"verdict": "pass", "has_blocking": True}))["ok"]


def test_parse_markdown_fenced():
    raw = 'Review:\n```json\n{"verdict": "pass", "has_blocking": false}\n```\nDone.'
    assert parse_qc_report(raw)["ok"]


def test_parse_bare_json():
    raw = 'Done. {"verdict": "fail", "has_blocking": false, "issues": []} All.'
    r = parse_qc_report(raw)
    assert r["verdict"] == "fail" and not r["ok"]


def test_parse_garbage():
    for bad in ("", "no json", "```code\nnot json\n```", None):
        r = parse_qc_report(bad)
        assert not r["ok"] and r["verdict"] == "fail"


def test_cli_build(capsys):
    assert main(["x", "build", "--summary", "impl X", "--files", "a.py,b.py", "--issue", "796"]) == 0  # fmt: skip
    assert "impl X" in json.loads(capsys.readouterr().out)["goal"]
    assert main(["x", "build", "--files", "a.py"]) == 2  # missing --summary


def test_cli_parse(capsys, monkeypatch):
    monkeypatch.setattr(sys, "stdin", io.StringIO(_j({"verdict": "pass", "has_blocking": False})))  # fmt: skip
    assert main(["x", "parse"]) == 0
    assert json.loads(capsys.readouterr().out)["ok"]
    monkeypatch.setattr(sys, "stdin", io.StringIO(_j({"verdict": "fail", "has_blocking": True})))  # fmt: skip
    assert main(["x", "parse"]) == 1


def test_cli_usage():
    assert main(["x"]) == 2 and main(["x", "frob"]) == 2
