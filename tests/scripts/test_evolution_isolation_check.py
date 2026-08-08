"""Tests for evolution_isolation_check.py (#1807, parent #1267)."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from evolution_isolation_check import (  # noqa: E402
    IsolationReport,
    Violation,
    format_report,
    main,
    run_isolation_check,
    scan_script,
)

GATE_AGENT = "def merge_verify(pr):\n    agent = AIAgent(c)\n    return agent.run()\n"
JUDGE_MUT = "def judge_eval(t):\n    s = 0\n    x = 1\n    y = 2\n    z = 3\n    t.conversation_history.append({'s': s})\n    return s\n"
GATE_IMPORT = "from eval_runner import run_task_set\n\ndef main():\n    pass\n"
CLEAN = "def merge_verify(pr):\n    r = subprocess.run(['python', 'eval.py'])\n    return r.returncode == 0\n"


def test_detects_coupling_patterns():
    cases = [
        (GATE_AGENT, "gate-instantiates-agent"),
        (JUDGE_MUT, "judge-mutates-conversation"),
        (GATE_IMPORT, "gate-imports-runner"),
    ]
    for src, pid in cases:
        assert pid in [v.pattern_id for v in scan_script("f", src)]


def test_clean_code_no_violations():
    assert scan_script("c", CLEAN) == []


def test_empty_none_and_nonexistent():
    assert scan_script("e", "") == [] and scan_script("n", None) == []
    assert run_isolation_check(["nope.py"]).scripts_scanned == []


def test_run_isolation_check_scans_pipeline():
    r = run_isolation_check()
    assert isinstance(r, IsolationReport) and r.scripts_scanned
    assert "eval_baseline.py" in r.scripts_scanned
    assert run_isolation_check(["eval_baseline.py"]).scripts_scanned == [
        "eval_baseline.py"
    ]


def test_format_pass_and_fail():
    assert "PASS" in format_report(IsolationReport(scripts_scanned=["a"]))
    t = format_report(
        IsolationReport(
            scripts_scanned=["a"], violations=[Violation("a", 10, "p", "d", "x")]
        )
    )
    assert "FAIL" in t and "a:10" in t


def test_main_and_strict(capsys):
    assert main(["--scripts", "nope.py"]) == 0
    assert "PASS" in capsys.readouterr().out
    assert main(["--strict"]) == 0
