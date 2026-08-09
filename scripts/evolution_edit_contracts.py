#!/usr/bin/env python3
"""Falsifiable edit contracts for closed-loop evolution (issue #1939, AHE).

Each evolution edit (skill/memory/prompt change) ships a four-field manifest
entry: failure evidence, root cause, targeted fix, and predicted impact (which
future cases should flip pass/fail). On the next cron cycle, the verification
step intersects predicted vs observed task deltas. Confirmed edits stay;
missed predictions are flagged for auto-revert at file granularity.

This script provides the manifest management and verification logic. The
auto-revert execution itself is deferred to integration (the merge gate
already handles git rollback). This script makes the evolution loop
falsifiable — every edit carries a testable prediction.

Usage:
    python scripts/evolution_edit_contracts.py [--evolution-dir DIR] record <json>
    python scripts/evolution_edit_contracts.py [--evolution-dir DIR] verify
    python scripts/evolution_edit_contracts.py [--evolution-dir DIR] list
"""

from __future__ import annotations
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

_REQUIRED_FIELDS = {
    "failure_evidence",
    "root_cause",
    "targeted_fix",
    "predicted_impact",
}
_SAFE_AUTO_REVERT = {"skill", "memory", "prompt", "config"}


def _default_evolution_dir() -> Path:
    env = os.environ.get("EVOLUTION_PROFILE_DIR", "").strip()
    if env:
        return Path(env)
    hermes_home = Path(
        os.environ.get("HERMES_HOME", "").strip() or (Path.home() / ".hermes")
    )
    return hermes_home / "evolution"


def _contracts_path(evolution_dir: Path | None = None) -> Path:
    return (evolution_dir or _default_evolution_dir()) / "edit_contracts.jsonl"


def validate_contract(contract: Dict[str, Any]) -> List[str]:
    """Validate a contract has all required fields. Returns list of errors."""
    errors: List[str] = []
    for field in _REQUIRED_FIELDS:
        if field not in contract or not contract[field]:
            errors.append(f"missing required field: {field}")
    # predicted_impact should specify which cases should flip.
    pi = contract.get("predicted_impact", {})
    if isinstance(pi, dict):
        if not pi.get("should_flip") and not pi.get("cases"):
            errors.append("predicted_impact must specify 'should_flip' or 'cases'")
    elif not pi:
        errors.append("predicted_impact must not be empty")
    return errors


def record_contract(
    contract: Dict[str, Any],
    evolution_dir: Path | None = None,
) -> Dict[str, Any]:
    """Record a falsifiable edit contract to the append-only log."""
    errors = validate_contract(contract)
    if errors:
        raise ValueError("; ".join(errors))
    entry = {
        "timestamp": contract.get("timestamp")
        or datetime.now(timezone.utc).isoformat(),
        "edit_type": contract.get("edit_type", "skill"),
        "file_path": contract.get("file_path", ""),
        "issue_number": contract.get("issue_number"),
        "pr_number": contract.get("pr_number"),
        "failure_evidence": contract["failure_evidence"],
        "root_cause": contract["root_cause"],
        "targeted_fix": contract["targeted_fix"],
        "predicted_impact": contract["predicted_impact"],
        "auto_revert": contract.get(
            "auto_revert", contract.get("edit_type", "skill") in _SAFE_AUTO_REVERT
        ),
        "verified": False,
        "verification_result": None,
    }
    path = _contracts_path(evolution_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, sort_keys=True) + "\n")
    return entry


def load_contracts(evolution_dir: Path | None = None) -> List[Dict[str, Any]]:
    """Load all contracts from the JSONL log."""
    path = _contracts_path(evolution_dir)
    if not path.exists():
        return []
    contracts: List[Dict[str, Any]] = []
    for ln in path.read_text(encoding="utf-8").splitlines():
        ln = ln.strip()
        if not ln:
            continue
        try:
            obj = json.loads(ln)
        except ValueError:
            continue
        if isinstance(obj, dict):
            contracts.append(obj)
    return contracts


def verify_contracts(
    observed_deltas: Dict[str, bool],
    evolution_dir: Path | None = None,
) -> List[Dict[str, Any]]:
    """Verify unverified contracts against observed task deltas.

    ``observed_deltas`` maps case identifiers to True (pass) / False (fail).
    A contract is 'confirmed' if the predicted flip matches the observed delta.
    A contract is 'missed' if the prediction didn't materialize.
    Returns the list of verified contracts with results.
    """
    contracts = load_contracts(evolution_dir)
    results: List[Dict[str, Any]] = []
    for c in contracts:
        if c.get("verified"):
            results.append(c)
            continue
        pi = c.get("predicted_impact", {})
        predicted_cases: List[str] = []
        if isinstance(pi, dict):
            predicted_cases = pi.get("cases", []) or [pi.get("should_flip", "")]
        confirmed = False
        missed = False
        for case in predicted_cases:
            if case and case in observed_deltas:
                if observed_deltas[case]:
                    confirmed = True
                else:
                    missed = True
        c["verified"] = True
        c["verification_result"] = (
            "confirmed"
            if confirmed and not missed
            else ("missed" if missed else "inconclusive")
        )
        results.append(c)
    # Re-write the log with verified results.
    path = _contracts_path(evolution_dir)
    if results:
        path.write_text(
            "\n".join(json.dumps(c, sort_keys=True) for c in results) + "\n",
            encoding="utf-8",
        )
    return results


def contracts_summary(contracts: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Summarize contract states for analytics."""
    by_type: Dict[str, int] = {}
    by_result: Dict[str, int] = {}
    for c in contracts:
        t = c.get("edit_type", "unknown")
        by_type[t] = by_type.get(t, 0) + 1
        r = c.get("verification_result") or "unverified"
        by_result[r] = by_result.get(r, 0) + 1
    return {"total": len(contracts), "by_type": by_type, "by_result": by_result}


def main(argv: List[str]) -> int:
    evolution_dir: Path | None = None
    args = argv[1:]
    if "--evolution-dir" in args:
        i = args.index("--evolution-dir")
        if i + 1 < len(args):
            evolution_dir = Path(args[i + 1])
            args = args[:i] + args[i + 2 :]
    if not args:
        s = contracts_summary(load_contracts(evolution_dir))
        print(
            f"[edit-contracts] {s['total']} contracts: by_type={s['by_type']} by_result={s['by_result']}"
        )
        return 0
    sub = args[0]
    if sub == "record":
        if len(args) < 2:
            print("error: record requires a JSON contract argument", file=sys.stderr)
            return 1
        result = record_contract(json.loads(args[1]), evolution_dir)
        print(
            f"[edit-contracts] recorded: {result['edit_type']} for #{result.get('issue_number')}"
        )
        return 0
    if sub == "verify":
        deltas = json.loads(args[1]) if len(args) > 1 else {}
        results = verify_contracts(deltas, evolution_dir)
        confirmed = sum(
            1 for r in results if r.get("verification_result") == "confirmed"
        )
        missed = sum(1 for r in results if r.get("verification_result") == "missed")
        print(f"[edit-contracts] verified: {confirmed} confirmed, {missed} missed")
        return 0
    if sub == "list":
        for c in load_contracts(evolution_dir):
            print(json.dumps(c))
        return 0
    print(f"unknown subcommand: {sub}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
