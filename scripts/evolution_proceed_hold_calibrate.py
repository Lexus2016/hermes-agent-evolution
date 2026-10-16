#!/usr/bin/env python3
"""Proceed-vs-hold calibration runner (#66).

This is the *wiring* slice for the proceed-vs-hold calibration suite: it makes
``evolution.lib.proceed_hold_calibration`` actually execute and persist results,
instead of sitting as an un-wired library (the reason PR #2824 was closed).

Two deciders are supported, matching the rework brief:

* **LLM decider** (the real product path) — an LLM cron session decides each
  labeled scenario PROCEED/HOLD and passes the decisions here via ``--decisions``.
  This script scores them with :func:`classify_outcome`, aggregates with
  :func:`run_calibration`, and appends the :class:`CalibrationReport` to the
  metrics trail. The LLM session *is* the "real pre-commit decider": it makes
  the same proceed-vs-hold call it would at an irreversible-action boundary
  (merge, mass delete, install, cron edit, payment, broadcast).

* **reference decider** — the deterministic evidence-weighted baseline. Kept
  only as the label-consistency check (it reproduces every ground-truth label),
  never as policy. Run with ``--reference`` — or with **no arguments at all**:
  the scheduler's pre-run data-collection slot invokes a job's ``script``
  arg-less, so a bare invocation defaults to the reference baseline and exits
  0 instead of hitting ``parser.error`` (which would inject a self-inflicted
  ``## Script Error`` into every scheduled run).

The script is pure and deterministic (no credentials, no LLM client, no MCP
dependency) so it is unit-testable and safe to run as ``no_agent`` for the
reference baseline. The LLM decisions are supplied by the cron session that
invokes it, mirroring the ``AgentJudgeGrader`` contract in
``scripts/evolution_rubric_judge.py``.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone, UTC
from pathlib import Path
from typing import Sequence

from evolution.lib.proceed_hold_calibration import (
    DEFAULT_SUITE,
    CalibrationReport,
    Decision,
    Scenario,
    reference_decider,
    run_calibration,
)

#: JSONL sidecar in the evolution dir that holds one calibration report per run.
REPORT_FILENAME = "proceed-hold-calibration.jsonl"

#: Scenario ids in suite order — used to map a positional decision list onto
#: the suite the LLM session is shown.
_DEFAULT_IDS = tuple(s.id for s in DEFAULT_SUITE)


def decider_from_decisions(decisions: Sequence[str | Decision]):
    """Build a ``Decider`` from one PROCEED/HOLD per scenario, in suite order.

    The LLM cron session supplies its decision per scenario in ``DEFAULT_SUITE``
    order; this adapter lets ``run_calibration`` score them identically to any
    other decider.
    """
    ordered = [
        d if isinstance(d, Decision) else Decision(str(d).strip().lower())
        for d in decisions
    ]
    if len(ordered) != len(_DEFAULT_IDS):
        raise ValueError(
            f"expected {len(_DEFAULT_IDS)} decisions (one per suite scenario), "
            f"got {len(ordered)}"
        )
    by_id = dict(zip(_DEFAULT_IDS, ordered))

    def _decide(scenario: Scenario) -> Decision:
        return by_id[scenario.id]

    return _decide


def build_report(decisions: Sequence[str | Decision]) -> CalibrationReport:
    """Score an LLM decision list and return the aggregate report."""
    return run_calibration(decider_from_decisions(decisions))


def persist_report(
    report: CalibrationReport,
    evolution_dir: Path,
    *,
    decider: str,
    timestamp: str | None = None,
) -> Path:
    """Append one JSON record (timestamp + decider + report) to the sidecar."""
    out = Path(evolution_dir) / REPORT_FILENAME
    out.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "timestamp": timestamp or datetime.now(UTC).isoformat(),
        "decider": decider,
        "report": report.to_dict(),
    }
    with out.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, sort_keys=True) + "\n")
    return out


def run_reference(evolution_dir: Path) -> CalibrationReport:
    """Run the deterministic baseline and persist it (label-consistency check)."""
    report = run_calibration(reference_decider)
    persist_report(report, evolution_dir, decider="reference")
    return report


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Score and persist a proceed-vs-hold calibration run (#66)."
    )
    parser.add_argument(
        "--decisions",
        help="comma-separated PROCEED/HOLD, one per DEFAULT_SUITE scenario in "
        "order (the LLM decider's answers).",
    )
    parser.add_argument(
        "--reference",
        action="store_true",
        help="run the deterministic reference baseline instead of an LLM decider.",
    )
    parser.add_argument(
        "--evolution-dir",
        default=str(Path.home() / ".hermes" / "evolution"),
        help="evolution directory to write the calibration sidecar into.",
    )
    args = parser.parse_args(argv)

    evolution_dir = Path(args.evolution_dir)
    if args.reference:
        report = run_calibration(reference_decider)
        decider = "reference"
    elif args.decisions:
        decisions = [x.strip() for x in args.decisions.split(",") if x.strip()]
        report = build_report(decisions)
        decider = "llm"
    else:
        # Arg-less invocation is the scheduler's pre-run data-collection path:
        # cron.scheduler._run_job_script executes a job's ``script`` with no
        # arguments and injects its stdout as context. parser.error here would
        # exit 2 and inject a self-inflicted "## Script Error" into every
        # scheduled run. With no decisions supplied, the only decider that can
        # run is the deterministic reference baseline — the label-consistency
        # check — so default to it and exit 0.
        report = run_calibration(reference_decider)
        decider = "reference"

    path = persist_report(report, evolution_dir, decider=decider)
    print(report.summary())
    print(f"wrote {path}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
