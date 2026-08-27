#!/usr/bin/env python3
"""Gated apply-path for harness code-diff proposals (#2615, #2525).

Slice C: wire the validated code-diff apply-path (Slice A generator + Slice B
sandbox/regression gate) into the evolution loop behind a MANUAL/CRON trigger —
never a silent self-modifying loop.

* **Manual**: ``evolution_harness_gate.py proposal.json [--out F] [--apply]``
  gates one Slice-A proposal; ``--apply`` writes the surface ONLY on green and
  requires ``--out``.
* **Cron**: zero-arg mode — the registered ``evolution-harness-gate``
  no_agent job (``cron/evolution/harness-gate.yaml``). Reads the trace
  miner's ``weaknesses-latest.json`` sidecar from ``EVOLUTION_PROFILE_DIR``,
  generates retry-policy proposals (offline, no LLM), gates each, and writes a
  ``harness-gate-latest.json`` report sidecar. REPORT-ONLY: cron never applies;
  ``--apply`` stays a deliberate human action.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from evolution_harness_proposer import generate_proposals, load_weaknesses
from evolution_harness_sandbox import INVALID, VALIDATED, validate_code_diff
from evolution_harness_validator import (
    DEFAULT_MIN_SESSIONS,
    score_candidate,
)
from evolution_invariant_filter import check_proposal

EXIT_VALIDATED = 0
EXIT_REJECTED = 1
EXIT_INVALID = 2
EXIT_APPLY_REFUSED = 3

#: Sidecar the cron mode writes next to the miner's weaknesses-latest.json.
GATE_SIDECAR = "harness-gate-latest.json"


def run_gate(
    proposal: Dict[str, Any],
    *,
    gate_runner=None,
    holdout_batch: Optional[List[Dict[str, Any]]] = None,
    min_sessions: int = DEFAULT_MIN_SESSIONS,
) -> Dict[str, Any]:
    """Sandboxed apply + regression gate for a proposal; never auto-applies.

    1. Screen against immutable invariant rules first (#68).
    2. Screen against held-out trace validation (#3228) to prevent overfitting.
    3. Run sandbox/regression validation (#2615).
    """
    invariant = check_proposal(proposal)
    if not invariant["ok"]:
        return {
            "status": INVALID,
            "applied": None,
            "gate": {"passed": False, "exit_code": None, "output": ""},
            "reason": "invariant violation",
            "zero_fitness": True,
            "violations": invariant["violations"],
            "requires_human_review": True,
            "auto_apply": False,
        }

    # Harness validator screen (#3228): ensure candidate generalizes across holdout batch
    candidate_info = proposal.get("candidate") or proposal.get("evidence") or proposal.get("weakness")
    batch = holdout_batch if holdout_batch is not None else proposal.get("holdout_batch")
    if candidate_info and batch is not None:
        val = score_candidate(candidate_info, batch, min_sessions=min_sessions)
        if val.get("verdict") == "reject":
            return {
                "status": INVALID,
                "applied": None,
                "gate": {"passed": False, "exit_code": None, "output": ""},
                "reason": f"harness validation rejected (overfitting risk): {val.get('key')}",
                "validator": val,
                "zero_fitness": True,
                "requires_human_review": False,
                "auto_apply": False,
            }

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


def run_cron_pass(
    weaknesses_payload: Any,
    *,
    gate_runner=None,
    surface: Optional[Dict[str, Any]] = None,
    holdout_batch: Optional[List[Dict[str, Any]]] = None,
    min_sessions: int = DEFAULT_MIN_SESSIONS,
) -> Dict[str, Any]:
    """Cron pass: weaknesses -> proposals -> gated verdicts (report-only)."""
    proposals = generate_proposals(load_weaknesses(weaknesses_payload), surface=surface)
    verdicts: List[Dict[str, Any]] = []
    skipped = 0
    invariant_rejected = 0
    validator_rejected = 0
    for p in proposals:
        # Slice 2 wiring (#68): screen the FULL proposal (title/delta/auto_apply)
        # against the immutable invariant rules before it reaches the gate.
        invariant = check_proposal(p)
        if not invariant["ok"]:
            invariant_rejected += 1
            verdicts.append({
                "status": INVALID,
                "applied": None,
                "gate": {"passed": False, "exit_code": None, "output": ""},
                "reason": "invariant violation",
                "zero_fitness": True,
                "violations": invariant["violations"],
                "proposal_title": p.get("title", ""),
                "requires_human_review": True,
                "auto_apply": False,
            })
            continue

        # Hold-out validation (#3228): ensure candidate generalizes
        cand = p.get("candidate") or p.get("evidence") or p.get("weakness")
        if cand and holdout_batch is not None:
            val = score_candidate(cand, holdout_batch, min_sessions=min_sessions)
            if val.get("verdict") == "reject":
                validator_rejected += 1
                verdicts.append({
                    "status": INVALID,
                    "applied": None,
                    "gate": {"passed": False, "exit_code": None, "output": ""},
                    "reason": f"harness validation rejected (overfitting risk): {val.get('key')}",
                    "validator": val,
                    "zero_fitness": True,
                    "proposal_title": p.get("title", ""),
                    "requires_human_review": False,
                    "auto_apply": False,
                })
                continue

        diff = p.get("code_diff")
        if not isinstance(diff, dict):
            skipped += 1  # gate only judges diffs it can sandbox-apply
            continue
        verdict = run_gate(
            diff,
            gate_runner=gate_runner,
            holdout_batch=holdout_batch,
            min_sessions=min_sessions,
        )
        verdict["proposal_title"] = p.get("title", "")
        verdicts.append(verdict)
    return {
        "mode": "cron-report-only",
        "auto_apply": False,
        "proposals": len(proposals),
        "gated": len(verdicts) - invariant_rejected - validator_rejected,
        "invariant_rejected": invariant_rejected,
        "validator_rejected": validator_rejected,
        "skipped_no_code_diff": skipped,
        "validated": sum(1 for v in verdicts if v.get("status") == VALIDATED),
        "verdicts": verdicts,
    }


def _cron_main() -> int:
    """Zero-arg scheduled pass. Silent (no stdout) when nothing is pending."""
    import os

    env = os.environ.get("EVOLUTION_PROFILE_DIR", "").strip()
    if not env:
        return EXIT_VALIDATED  # no profile configured this tick
    prof = Path(env)
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
        # A green gate is the one event worth surfacing; applying stays human.
        print(
            f"[harness-gate] {report['validated']}/{report['gated']} proposal(s) "
            f"passed the regression gate — see {GATE_SIDECAR} (apply is manual)"
        )
    return EXIT_VALIDATED


def main(argv: List[str]) -> int:
    positional = [a for a in argv[1:] if not a.startswith("-")]
    if not positional:
        return _cron_main()  # scheduled (zero-arg) invocation

    proposal_path, out, apply = positional[0], None, "--apply" in argv
    for i, a in enumerate(argv[1:], 1):
        if a == "--out" and i + 1 < len(argv):
            out = argv[i + 1]

    try:
        proposal = json.loads(Path(proposal_path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(json.dumps({"status": INVALID, "reason": f"cannot read proposal: {exc}"}))
        return EXIT_INVALID

    verdict = run_gate(proposal)
    print(json.dumps(verdict, sort_keys=True))
    if verdict.get("status") == INVALID:
        return EXIT_INVALID
    if verdict.get("status") != VALIDATED:
        return EXIT_REJECTED
    if not apply:
        return EXIT_VALIDATED  # dry-run: verdict only, nothing written
    if not out:
        print(json.dumps({"status": "refused", "reason": "--apply requires --out"}))
        return EXIT_APPLY_REFUSED
    if apply_validated(verdict, Path(out)):
        print(json.dumps({"status": "applied", "out": str(Path(out))}))
        return EXIT_VALIDATED
    print(
        json.dumps({"status": "refused", "reason": "gate not green; nothing applied"})
    )
    return EXIT_APPLY_REFUSED


if __name__ == "__main__":
    sys.exit(main(sys.argv))
