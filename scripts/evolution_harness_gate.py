"""Gated apply-path entry point for harness code-diff proposals (#2615, #2525).

Slice C: wire the validated code-diff apply-path (Slice A + B) into the
evolution loop behind a MANUAL/CRON trigger — never a silent self-modifying
loop. Reads a Slice-A proposal (code_diff JSON), routes it through the
sandboxed apply + regression gate (Slice B), prints a machine-readable verdict,
and ONLY with an explicit ``--apply`` flag AND a green gate writes the
validated surface to the target file.
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

EXIT_VALIDATED = 0
EXIT_REJECTED = 1
EXIT_INVALID = 2
EXIT_APPLY_REFUSED = 3

GateRunner = Callable[[Dict[str, Any]], Any]


def run_gate(
    proposal: Dict[str, Any],
    *,
    repo_root: Optional[Path] = None,
    timeout: int = 300,
    gate_runner: Optional[GateRunner] = None,
) -> Dict[str, Any]:
    """Sandboxed apply + regression gate for a proposal; never auto-applies."""
    if gate_runner is None:

        def _runner(applied: Dict[str, Any]) -> Any:
            return default_gate_runner(applied, repo_root=repo_root, timeout=timeout)

        gate_runner = _runner
    return validate_code_diff(proposal, gate_runner=gate_runner)


def apply_validated(verdict: Dict[str, Any], out_path: Path) -> bool:
    """Write the validated surface to *out_path* — ONLY when the gate is green.

    Returns True when written; refuses rejected/invalid proposals outright.
    """
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
    parser.add_argument("proposal", help="path to a Slice-A proposal JSON (code_diff)")
    parser.add_argument(
        "--out", help="target path for the validated surface (required with --apply)"
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="write the surface ONLY when the regression gate is green",
    )
    parser.add_argument("--repo-root", type=Path, default=None)
    parser.add_argument("--timeout", type=int, default=300)
    args = parser.parse_args(argv)

    try:
        with open(args.proposal, encoding="utf-8") as fh:
            proposal = json.load(fh)
    except (OSError, ValueError) as exc:
        print(
            json.dumps(
                {
                    "status": INVALID,
                    "reason": f"cannot read proposal: {exc}",
                    "requires_human_review": True,
                    "auto_apply": False,
                }
            )
        )
        return EXIT_INVALID

    verdict = run_gate(proposal, repo_root=args.repo_root, timeout=args.timeout)
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
    print(json.dumps({"status": "refused", "reason": "gate not green; nothing applied"}))
    return EXIT_APPLY_REFUSED


if __name__ == "__main__":
    sys.exit(main(sys.argv))
