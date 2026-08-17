"""Gated apply-path for harness code-diff proposals (#2615, #2525).

Slice C: wire the validated code-diff apply-path (Slice A + B) behind a
MANUAL trigger — never a silent self-modifying loop. Only an explicit
``--apply`` AND a green gate write the validated surface.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from evolution_harness_sandbox import (
    INVALID,
    REJECTED,
    VALIDATED,
    default_gate_runner,
    validate_code_diff,
)

EXIT_VALIDATED, EXIT_REJECTED, EXIT_INVALID, EXIT_APPLY_REFUSED = 0, 1, 2, 3
GateRunner = Callable[[Dict[str, Any]], Any]


def run_gate(
    proposal: Dict[str, Any],
    *,
    repo_root: Optional[Path] = None,
    timeout: int = 300,
    gate_runner: Optional[GateRunner] = None,
) -> Dict[str, Any]:
    """Sandboxed apply + regression gate; never auto-applies."""
    if gate_runner is None:
        gate_runner = lambda applied: default_gate_runner(  # noqa: E731
            applied, repo_root=repo_root, timeout=timeout
        )
    return validate_code_diff(proposal, gate_runner=gate_runner)


def apply_validated(verdict: Dict[str, Any], out_path: Path) -> bool:
    """Write the validated surface — ONLY when the gate is green."""
    if verdict.get("status") != VALIDATED or not verdict.get("applied"):
        return False
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(verdict["applied"]["after"], indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return True


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description="Gated apply-path for harness code-diff proposals (#2615)"
    )
    parser.add_argument("proposal", help="Slice-A proposal JSON (code_diff)")
    parser.add_argument("--out", help="target path for the surface (needs --apply)")
    parser.add_argument("--apply", action="store_true", help="write only when green")
    args = parser.parse_args(argv)

    try:
        with open(args.proposal, encoding="utf-8") as fh:
            proposal = json.load(fh)
    except (OSError, ValueError) as exc:
        print(
            json.dumps({
                "status": INVALID,
                "reason": f"cannot read proposal: {exc}",
                "requires_human_review": True,
                "auto_apply": False,
            })
        )
        return EXIT_INVALID

    verdict = run_gate(proposal)
    print(json.dumps(verdict, sort_keys=True))

    if verdict.get("status") != VALIDATED:
        return EXIT_INVALID if verdict.get("status") == INVALID else EXIT_REJECTED

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
