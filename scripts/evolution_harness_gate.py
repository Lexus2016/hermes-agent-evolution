#!/usr/bin/env python3
"""Gated apply-path entry point for harness code-diff proposals (#2615, #2525).

Slice C: wire the validated code-diff apply-path (Slice A generator + Slice B
sandbox/regression gate) into the evolution loop behind a MANUAL/CRON trigger —
never a silent self-modifying loop.

Two entry points:

* **Manual** — ``evolution_harness_gate.py proposal.json [--out FILE] [--apply]``
  routes one Slice-A proposal through ``validate_code_diff`` (sandboxed apply +
  regression gate), prints a machine-readable verdict, and ONLY with an explicit
  ``--apply`` AND a green gate writes the validated surface to ``--out``.
* **Cron** — registered as the ``evolution-harness-gate`` no_agent job
  (``cron/evolution/harness-gate.yaml``, picked up by
  ``scripts/register_evolution_cron.py``). Zero-arg mode: read the trace
  miner's ``weaknesses-latest.json`` sidecar, generate retry-policy proposals
  via the Slice-A proposer, gate each one, and write a ``harness-gate-latest.json``
  verdicts sidecar. Cron is REPORT-ONLY — it never applies anything; ``--apply``
  stays a deliberate human action.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from evolution_harness_proposer import generate_proposals, load_weaknesses
from evolution_harness_sandbox import (
    INVALID,
    VALIDATED,
    validate_code_diff,
)

EXIT_VALIDATED = 0
EXIT_REJECTED = 1
EXIT_INVALID = 2
EXIT_APPLY_REFUSED = 3

#: Sidecar the cron mode writes next to the miner's weaknesses-latest.json.
GATE_SIDECAR = "harness-gate-latest.json"


def run_gate(proposal: Dict[str, Any], *, gate_runner=None) -> Dict[str, Any]:
    """Sandboxed apply + regression gate for a proposal; never auto-applies."""
    return validate_code_diff(proposal, gate_runner=gate_runner)


def apply_validated(verdict: Dict[str, Any], out_path: Path) -> bool:
    """Write the validated surface to *out_path* — ONLY when the gate is green."""
    if verdict.get("status") != VALIDATED or not verdict.get("applied"):
        return False
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(verdict["applied"]["after"], indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return True


def _profile_dir() -> Optional[Path]:
    import os

    env = os.environ.get("EVOLUTION_PROFILE_DIR", "").strip()
    return Path(env) if env else None


def run_cron_pass(
    weaknesses_payload: Any,
    *,
    gate_runner=None,
    surface: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Cron pass: weaknesses -> proposals -> gated verdicts (report-only).

    Deterministic, no LLM (the offline proposer envelope), no writes to any
    live config. Proposals without a ``code_diff`` are counted as ``skipped`` —
    the gate only judges structured diffs it can sandbox-apply.
    """
    proposals = generate_proposals(load_weaknesses(weaknesses_payload), surface=surface)
    verdicts: List[Dict[str, Any]] = []
    skipped = 0
    for p in proposals:
        diff = p.get("code_diff")
        if not isinstance(diff, dict):
            skipped += 1
            continue
        verdict = run_gate(diff, gate_runner=gate_runner)
        verdict["proposal_title"] = p.get("title", "")
        verdicts.append(verdict)
    return {
        "mode": "cron-report-only",
        "auto_apply": False,
        "proposals": len(proposals),
        "gated": len(verdicts),
        "skipped_no_code_diff": skipped,
        "validated": sum(1 for v in verdicts if v.get("status") == VALIDATED),
        "verdicts": verdicts,
    }


def _cron_main() -> int:
    """Zero-arg scheduled pass. Silent (no stdout) when nothing is pending."""
    prof = _profile_dir()
    if prof is None:
        return EXIT_VALIDATED  # no profile configured this tick — nothing to do
    src = prof / "weaknesses-latest.json"
    if not src.is_file():
        return EXIT_VALIDATED  # miner has not run yet — nothing to gate
    try:
        payload = json.loads(src.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return EXIT_INVALID  # unreadable sidecar — surface as a cron error
    report = run_cron_pass(payload, surface={})  # {} = proposer default surface
    try:
        (prof / GATE_SIDECAR).write_text(
            json.dumps(report, indent=2, sort_keys=True), encoding="utf-8"
        )
    except OSError:
        return EXIT_INVALID
    if report["validated"]:
        # A green gate is the one event worth surfacing to the job log; the
        # apply decision itself stays with a human.
        print(
            f"[harness-gate] {report['validated']}/{report['gated']} proposal(s) "
            f"passed the regression gate — see {GATE_SIDECAR} (apply is manual)"
        )
    return EXIT_VALIDATED


def main(argv: List[str]) -> int:
    if (
        len([a for a in argv[1:] if not a.startswith("-")]) == 0
        and "--apply" not in argv
    ):
        return _cron_main()

    import argparse

    parser = argparse.ArgumentParser(description="Gated apply-path (#2615)")
    parser.add_argument("proposal", help="path to a Slice-A proposal JSON (code_diff)")
    parser.add_argument("--out", help="target path for the validated surface")
    parser.add_argument(
        "--apply", action="store_true", help="write the surface ONLY on a green gate"
    )
    args = parser.parse_args(argv[1:])

    try:
        proposal = json.loads(Path(args.proposal).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(json.dumps({"status": INVALID, "reason": f"cannot read proposal: {exc}"}))
        return EXIT_INVALID

    verdict = run_gate(proposal)
    print(json.dumps(verdict, sort_keys=True))
    if verdict.get("status") == INVALID:
        return EXIT_INVALID
    if verdict.get("status") != VALIDATED:
        return EXIT_REJECTED
    if not args.apply:
        return EXIT_VALIDATED  # dry-run: verdict only, nothing written
    if not args.out:
        print(json.dumps({"status": "refused", "reason": "--apply requires --out"}))
        return EXIT_APPLY_REFUSED
    if apply_validated(verdict, Path(args.out)):
        print(json.dumps({"status": "applied", "out": str(Path(args.out))}))
        return EXIT_VALIDATED
    print(
        json.dumps({"status": "refused", "reason": "gate not green; nothing applied"})
    )
    return EXIT_APPLY_REFUSED


if __name__ == "__main__":
    sys.exit(main(sys.argv))
