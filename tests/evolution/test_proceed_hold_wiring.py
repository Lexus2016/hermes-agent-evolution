"""Wiring tests for the proceed-vs-hold calibration runner (#66).

These prove the calibration module is no longer dead code: the runner builds a
report from a decider, persists it end-to-end to the JSONL sidecar, and the
reference baseline reproduces every label. (PR #2824 was closed for shipping
the module with zero call sites — this is the wiring that fixes that.)
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

# scripts/ is not a package — import the runner the same way sibling evolution
# tests import evolution_* scripts (flat, after adding scripts/ to sys.path).
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from evolution_proceed_hold_calibrate import (
    REPORT_FILENAME,
    build_report,
    decider_from_decisions,
    persist_report,
    run_reference,
)
from evolution.lib.proceed_hold_calibration import (
    DEFAULT_SUITE,
    Decision,
    reference_decider,
    run_calibration,
)


def _truth_counts():
    proceed = sum(1 for s in DEFAULT_SUITE if s.ground_truth is Decision.PROCEED)
    hold = sum(1 for s in DEFAULT_SUITE if s.ground_truth is Decision.HOLD)
    return proceed, hold


def test_decider_from_decisions_reproduces_reference():
    # Feeding the reference decider's own decisions back through the adapter
    # must score a perfect report — proves the positional mapping is correct.
    decisions = [reference_decider(s) for s in DEFAULT_SUITE]
    report = run_calibration(decider_from_decisions(decisions))
    assert report.correct == len(DEFAULT_SUITE)


def test_decider_from_decisions_rejects_wrong_length():
    import pytest

    with pytest.raises(ValueError):
        decider_from_decisions([Decision.PROCEED])  # too short


def test_build_report_scores_llm_decision_list():
    # An LLM that always holds: every safe PROCEED truth becomes an over-refusal.
    proceed_truth, hold_truth = _truth_counts()
    report = build_report([Decision.HOLD] * len(DEFAULT_SUITE))
    assert report.correct == hold_truth
    assert report.over_refusal == proceed_truth
    assert report.under_refusal == 0


def test_persist_report_writes_jsonl_round_trip(tmp_path: Path):
    report = build_report([Decision.HOLD] * len(DEFAULT_SUITE))
    path = persist_report(
        report, tmp_path, decider="llm", timestamp="2026-08-19T00:00:00+00:00"
    )
    assert path == tmp_path / REPORT_FILENAME
    lines = [l for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert rec["decider"] == "llm"
    assert rec["timestamp"] == "2026-08-19T00:00:00+00:00"
    assert rec["report"]["over_refusal"] > 0


def test_reference_run_end_to_end(tmp_path: Path):
    # The full no-LLM path: run + persist, then read the record back.
    report = run_reference(tmp_path)
    assert report.correct == len(DEFAULT_SUITE)
    assert report.under_refusal == 0
    assert report.over_refusal == 0
    rec = json.loads(
        (tmp_path / REPORT_FILENAME).read_text(encoding="utf-8").splitlines()[-1]
    )
    assert rec["decider"] == "reference"
    assert rec["report"]["correct"] == len(DEFAULT_SUITE)


def test_argless_invocation_is_scheduler_prerun_safe(tmp_path: Path):
    """The scheduler's pre-run data-collection slot (cron.scheduler
    _run_job_script) executes a job's ``script`` with NO arguments and treats
    a non-zero exit as failure, injecting a self-inflicted "## Script Error"
    into the LLM prompt (rework brief item 2). A bare invocation must therefore
    run the reference baseline and exit 0 — never parser.error (exit 2).
    """
    repo_root = Path(__file__).resolve().parents[2]
    env = os.environ.copy()
    # Redirect the reference report write away from the real ~/.hermes.
    env["HOME"] = str(tmp_path)
    # Resolve evolution.lib from this tree even under `python scripts/...`
    # (where the script's own dir, not the repo root, heads sys.path).
    env["PYTHONPATH"] = str(repo_root)
    proc = subprocess.run(
        [
            sys.executable,
            str(repo_root / "scripts" / "evolution_proceed_hold_calibrate.py"),
        ],
        capture_output=True,
        text=True,
        env=env,
        cwd=repo_root,
        timeout=60,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr or proc.stdout
    combined = f"{proc.stdout}\n{proc.stderr}".lower()
    assert "parser.error" not in combined
    assert "usage:" not in combined
    # It really ran the label-consistency baseline and persisted it.
    sidecar = tmp_path / ".hermes" / "evolution" / REPORT_FILENAME
    assert sidecar.is_file()
    rec = json.loads(sidecar.read_text(encoding="utf-8").splitlines()[-1])
    assert rec["decider"] == "reference"
    assert rec["report"]["correct"] == len(DEFAULT_SUITE)
    assert rec["report"]["under_refusal"] == 0
