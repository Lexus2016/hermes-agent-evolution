#!/usr/bin/env python3
"""Falsifiable edit contracts for closed-loop evolution (issue #1939, AHE).

Each edit ships a manifest (failure evidence, root cause, targeted fix,
predicted impact). The verify step intersects predicted vs observed deltas.

Usage: evolution_edit_contracts.py [--evolution-dir DIR] record <json>
       evolution_edit_contracts.py [--evolution-dir DIR] verify [--deltas <json>]
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

_REQUIRED = {"failure_evidence", "root_cause", "targeted_fix", "predicted_impact"}
_SAFE_REVERT = {"skill", "memory", "prompt", "config"}


def _path(d: Path | None = None) -> Path:
    if d is not None:
        return d / "edit_contracts.jsonl"
    env = os.environ.get("EVOLUTION_PROFILE_DIR", "").strip()
    home = env or os.environ.get("HERMES_HOME", "").strip() or (Path.home() / ".hermes")
    return Path(home) / "evolution" / "edit_contracts.jsonl"


def validate_contract(c: Dict[str, Any]) -> List[str]:
    """Validate required fields; returns error list."""
    errs = [f"missing required field: {f}" for f in _REQUIRED if not c.get(f)]
    pi = c.get("predicted_impact", {})
    if isinstance(pi, dict) and not pi.get("should_flip") and not pi.get("cases"):
        errs.append("predicted_impact must specify 'should_flip' or 'cases'")
    return errs


def record_contract(c: Dict[str, Any], d: Path | None = None) -> Dict[str, Any]:
    """Record a falsifiable edit contract to the append-only JSONL log."""
    errs = validate_contract(c)
    if errs:
        raise ValueError("; ".join(errs))
    et = c.get("edit_type", "skill")
    entry = {
        "timestamp": c.get("timestamp") or datetime.now(timezone.utc).isoformat(),
        "edit_type": et,
        "file_path": c.get("file_path", ""),
        "issue_number": c.get("issue_number"),
        "pr_number": c.get("pr_number"),
        "failure_evidence": c["failure_evidence"],
        "root_cause": c["root_cause"],
        "targeted_fix": c["targeted_fix"],
        "predicted_impact": c["predicted_impact"],
        "auto_revert": c.get("auto_revert", et in _SAFE_REVERT),
        "prior_path": c.get("prior_path", ""),
        "prior_content": c.get("prior_content"),
        "verified": False,
        "verification_result": None,
    }
    p = _path(d)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, sort_keys=True) + "\n")
    return entry


def load_contracts(d: Path | None = None) -> List[Dict[str, Any]]:
    """Load all contracts from the JSONL log."""
    p = _path(d)
    if not p.exists():
        return []
    out: List[Dict[str, Any]] = []
    for ln in p.read_text(encoding="utf-8").splitlines():
        ln = ln.strip()
        if not ln:
            continue
        try:
            obj = json.loads(ln)
        except ValueError:
            continue
        if isinstance(obj, dict):
            out.append(obj)
    return out


def verify_contracts(
    observed: Dict[str, bool], d: Path | None = None
) -> List[Dict[str, Any]]:
    """Verify unverified contracts against observed task deltas."""
    contracts = load_contracts(d)
    for c in contracts:
        if c.get("verified"):
            continue
        pi = c.get("predicted_impact", {})
        preds = pi.get("cases", []) if isinstance(pi, dict) else []
        if not preds and isinstance(pi, dict) and pi.get("should_flip"):
            preds = [pi["should_flip"]]
        confirmed = any(observed.get(pc) for pc in preds if pc in observed)
        missed = any(observed.get(pc) is False for pc in preds if pc in observed)
        c["verified"] = True
        c["verification_result"] = (
            "confirmed"
            if confirmed and not missed
            else ("missed" if missed else "inconclusive")
        )
    p = _path(d)
    if contracts:
        p.write_text(
            "\n".join(json.dumps(c, sort_keys=True) for c in contracts) + "\n",
            encoding="utf-8",
        )
    return contracts


class RestoreRefused(ValueError):
    """A contract names a note that is not inside the notes root."""


def _inside(root: Path, target: Path) -> bool:
    """True when ``target`` resolves to a path under ``root``."""
    try:
        target.relative_to(root)
    except ValueError:
        return False
    return True


def capture_note(path: str | Path) -> str:
    """Read the note exactly as it is on disk, for a later verbatim restore.

    Call this BEFORE the edit replaces the note; pass the return value as
    ``prior_content`` and the path as ``prior_path`` to :func:`record_contract`.
    """
    return Path(path).read_text(encoding="utf-8")


def restore_missed_contracts(
    notes_root: str | Path,
    d: Path | None = None,
    apply: bool = False,
) -> List[Dict[str, Any]]:
    """Put the previous note back when a contract's predicted gain did not land.

    The rule is deliberately narrow (issue #3554): an edit is reverted only
    when the verification step already called it ``missed``, the contract was
    recorded with ``auto_revert`` for a revertible edit type, and the previous
    note text was captured at record time. The restore is byte-for-byte — the
    file is rewritten with the captured string, not with a regenerated note.

    ``notes_root`` bounds where a restore may write. A contract whose
    ``prior_path`` resolves outside that root is skipped and marked
    ``restore_skipped`` instead, so a poisoned or malformed contract cannot
    reach an arbitrary file.

    ``apply=False`` (the default) reports what would be restored without
    touching the disk.
    """
    contracts = load_contracts(d)
    root = Path(notes_root).resolve()
    actions: List[Dict[str, Any]] = []
    for c in contracts:
        if c.get("restored"):
            continue
        if c.get("verification_result") != "missed":
            continue
        if not c.get("auto_revert"):
            continue
        prior = c.get("prior_content")
        target_raw = c.get("prior_path")
        if not isinstance(prior, str) or not target_raw:
            continue
        target = Path(target_raw)
        try:
            resolved = target.resolve()
        except OSError:
            c["restore_skipped"] = "unresolvable_path"
            actions.append(c)
            continue
        if not _inside(root, resolved):
            c["restore_skipped"] = "outside_notes_root"
            actions.append(c)
            continue
        action = {
            "issue_number": c.get("issue_number"),
            "path": str(resolved),
            "bytes": len(prior.encode("utf-8")),
            "applied": False,
        }
        if apply:
            resolved.parent.mkdir(parents=True, exist_ok=True)
            resolved.write_text(prior, encoding="utf-8")
            c["restored"] = True
            c["restored_at"] = datetime.now(timezone.utc).isoformat()
            action["applied"] = True
        c.pop("restore_skipped", None)
        actions.append(action)
    if actions and apply:
        p = _path(d)
        if contracts:
            p.write_text(
                "\n".join(json.dumps(c, sort_keys=True) for c in contracts) + "\n",
                encoding="utf-8",
            )
    return actions


def contracts_summary(contracts: List[Dict[str, Any]]) -> Dict[str, Any]:
    bt: Dict[str, int] = {}
    br: Dict[str, int] = {}
    for c in contracts:
        k = c.get("edit_type", "?")
        bt[k] = bt.get(k, 0) + 1
        r = c.get("verification_result") or "unverified"
        br[r] = br.get(r, 0) + 1
    return {"total": len(contracts), "by_type": bt, "by_result": br}


def main(argv: List[str]) -> int:
    d: Path | None = None
    args = argv[1:]
    if "--evolution-dir" in args:
        i = args.index("--evolution-dir")
        if i + 1 < len(args):
            d = Path(args[i + 1])
            args = args[:i] + args[i + 2 :]
    if not args:
        s = contracts_summary(load_contracts(d))
        print(f"[edit-contracts] {s['total']}: {s['by_type']} {s['by_result']}")
    elif args[0] == "record":
        r = record_contract(json.loads(args[1]), d)
        print(f"[edit-contracts] recorded: {r['edit_type']} #{r.get('issue_number')}")
    elif args[0] == "restore":
        if len(args) < 2:
            print("restore needs a notes root", file=sys.stderr)
            return 1
        acts = restore_missed_contracts(args[1], d, apply="--apply" in args)
        verb = "restored" if "--apply" in args else "would restore"
        if not acts:
            print("[edit-contracts] nothing to restore")
        for a in acts:
            if a.get("restore_skipped"):
                print(
                    f"[edit-contracts] skipped #{a.get('issue_number')}: "
                    f"{a['restore_skipped']}"
                )
            elif "applied" in a:
                print(
                    f"[edit-contracts] {verb} #{a['issue_number']} "
                    f"-> {a['path']} ({a['bytes']} bytes)"
                )
    elif args[0] == "verify":
        obs = json.loads(args[1]) if len(args) > 1 else {}
        results = verify_contracts(obs, d)
        c = sum(1 for r in results if r.get("verification_result") == "confirmed")
        m = sum(1 for r in results if r.get("verification_result") == "missed")
        print(f"[edit-contracts] verified: {c} confirmed, {m} missed")
    else:
        print(f"unknown: {args[0]}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
