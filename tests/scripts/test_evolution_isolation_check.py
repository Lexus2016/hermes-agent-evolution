"""Tests for evolution_isolation_check.py — BenchJack isolation defense (#1807)."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
from evolution_isolation_check import (  # noqa: E402  # fmt: skip
    Finding,
    ScanResult,
    scan_file,
    run_scan,
    format_report,
    main,
)


def test_qc_review_isolated():
    findings = scan_file("evolution_qc_review.py")
    assert any(f.severity == "info" for f in findings)
    assert not any(f.severity == "high" for f in findings)


def test_orchestrator_isolated():
    findings = scan_file("evolution_orchestrator.py")
    assert any(f.severity == "info" for f in findings)


def test_missing_file():
    assert scan_file("nonexistent.py") == []


def test_full_scan_no_coupling():
    r = run_scan()
    assert len(r.scripts_scanned) == 4
    assert not r.has_failures  # no coupling in real pipeline
    assert len([f for f in r.findings if f.severity == "info"]) >= 2


def test_report_pass():
    assert "RESULT: PASS" in format_report(run_scan())


def test_report_fail():
    r = ScanResult(findings=[Finding("evaluator-isolation", "high", "x.py", "COUPLING")])  # fmt: skip
    assert "RESULT: FAIL" in format_report(r)


def test_cli_text(capsys):
    assert main(["x"]) == 0
    assert "Isolation" in capsys.readouterr().out


def test_cli_json(capsys):
    assert main(["x", "--json"]) == 0
    d = json.loads(capsys.readouterr().out)
    assert not d["has_failures"]
    assert len(d["findings"]) >= 2