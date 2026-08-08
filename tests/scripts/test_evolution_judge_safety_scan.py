"""Tests for evolution_judge_safety_scan.py (#1808, parent #1267)."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from evolution_judge_safety_scan import (  # noqa: E402
    SafetyReport,
    SafetyViolation,
    format_report,
    main,
    run_safety_scan,
    scan_script,
)

UNSAFE = 'JUDGE_PROMPT = f"""\nYou are evaluating this implementation:\n{implementation}\nScore it 1-10.\n"""'
SAFE = 'JUDGE_PROMPT = f"""\nYou are evaluating:\n<data>{implementation}</data>\nScore 1-10.\n"""'
CLEAN = 'PROMPT = "Evaluate the code quality on a scale of 1-10."\n'


def test_detects_unsafe_interpolation():
    v = scan_script("f", UNSAFE)
    assert len(v) > 0 and "delimit" in v[0].issue.lower()


def test_safe_delimited_passes():
    assert scan_script("s", SAFE) == []


def test_clean_and_empty():
    assert scan_script("c", CLEAN) == []
    assert scan_script("e", "") == [] and scan_script("n", None) == []


def test_run_safety_scan():
    r = run_safety_scan()
    assert isinstance(r, SafetyReport) and r.scripts_scanned


def test_nonexistent_skipped():
    r = run_safety_scan(["nope.py"])
    assert r.scripts_scanned == [] and r.passed


def test_format_report():
    assert "PASS" in format_report(SafetyReport(scripts_scanned=["a"]))
    t = format_report(
        SafetyReport(
            scripts_scanned=["a"],
            violations=[SafetyViolation("a", 5, "f'{impl}'", "Unsafe.")],
        )
    )
    assert "FAIL" in t and "a:5" in t


def test_main_and_strict(capsys):
    assert main(["--scripts", "nope.py"]) == 0
    assert "PASS" in capsys.readouterr().out
