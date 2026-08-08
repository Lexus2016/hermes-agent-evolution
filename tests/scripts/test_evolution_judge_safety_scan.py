"""Tests for evolution_judge_safety_scan.py — prompt-injection defense (#1808)."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
from evolution_judge_safety_scan import (  # noqa: E402  # fmt: skip
    Finding,
    ScanResult,
    _DELIM_RE,
    run_scan,
    scan_file,
    format_report,
    main,
)


def test_delim_detection():
    assert _DELIM_RE.search("<untrusted>x</untrusted>")
    assert _DELIM_RE.search("<data>y</data>")
    assert not _DELIM_RE.search("plain {summary}")
    assert _DELIM_RE.search("<UNTRUSTED>z</UNTRUSTED>")


def test_qc_review_has_summary_injection():
    findings = scan_file("evolution_qc_review.py")
    assert any("summary" in f.description for f in findings)
    assert all(f.severity == "high" for f in findings)


def test_orchestrator_has_subtask_injection():
    assert len(scan_file("evolution_orchestrator.py")) >= 1


def test_evaluator_and_missing():
    assert scan_file("evolution_evaluator.py") == []
    assert scan_file("nope.py") == []


def test_full_scan():
    r = run_scan()
    assert len(r.scripts_scanned) == 5 and len(r.findings) >= 2 and r.has_failures
    assert "RESULT: FAIL" in format_report(r)
    assert "RESULT: PASS" in format_report(ScanResult(scripts_scanned=["x.py"]))


def test_cli(capsys):
    assert main(["x"]) == 1
    assert "finding" in capsys.readouterr().out
    assert main(["x", "--json"]) == 1
    d = json.loads(capsys.readouterr().out)
    assert d["has_failures"] and len(d["findings"]) >= 2


def test_to_dict():
    f = Finding("prompt-injection", "high", "x.py", 1, "d", "r")
    assert ScanResult(findings=[f]).to_dict()["has_failures"] is True
