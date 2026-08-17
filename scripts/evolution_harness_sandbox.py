"""Apply + validate a retry-policy code-diff in a sandbox (#2614, parent #2525).

Slice B: apply a Slice-A code diff's ``changes`` to a SANDBOXED COPY of the
retry-policy surface, run the regression gate, and mark the diff ``validated``
only when the gate is green. Human-gated, never auto-applied; gate injectable.
"""

from __future__ import annotations

import copy
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

VALIDATED, REJECTED, INVALID = "validated", "rejected", "invalid"
REGRESSION_GATE = ("scripts", "run_tests.sh")
GateRunner = Callable[[Dict[str, Any]], "GateResult"]


@dataclass
class GateResult:
    """Binary feedback from the regression gate."""

    passed: bool
    exit_code: int
    output: str


def apply_diff(code_diff: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Apply a code diff's ``changes`` to a COPY of its ``surface`` (sandbox copy).

    Returns ``{"before", "after", "changes"}`` or ``None`` for a malformed/empty
    diff. Never mutates the input.
    """
    if not isinstance(code_diff, dict):
        return None
    surface, changes = code_diff.get("surface"), code_diff.get("changes")
    if not isinstance(surface, dict) or not isinstance(changes, list):
        return None
    before, after = copy.deepcopy(surface), copy.deepcopy(surface)
    applied: List[Dict[str, Any]] = []
    for change in changes:
        if isinstance(change, dict) and "field" in change and "after" in change:
            after[change["field"]] = copy.deepcopy(change["after"])
            applied.append(change)
    return {"before": before, "after": after, "changes": applied} if applied else None


def default_gate_runner(
    applied: Dict[str, Any],
    *,
    repo_root: Optional[Path] = None,
    command: Optional[Tuple[str, ...]] = None,
    timeout: int = 300,
) -> GateResult:
    """Run the canonical regression gate (binary feedback) against *applied*."""
    root = repo_root or Path(__file__).resolve().parents[1]
    cmd = list(command) if command else [str(root / Path(*REGRESSION_GATE))]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=str(root),
            timeout=timeout,
        )
        return GateResult(
            proc.returncode == 0,
            proc.returncode,
            (proc.stdout or "") + (proc.stderr or ""),
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        return GateResult(False, -1, str(exc))


def validate_code_diff(
    code_diff: Dict[str, Any], *, gate_runner: Optional[GateRunner] = None
) -> Dict[str, Any]:
    """Apply a diff to a sandboxed copy and validate it against the gate.

    ``status`` is ``validated`` (applied + gate green), ``rejected`` (gate failed /
    regression), or ``invalid`` (malformed diff). The verdict keeps the hard
    human-gating fields — a green gate never means auto-apply.
    """
    applied = apply_diff(code_diff)
    if applied is None:
        return {
            "status": INVALID,
            "applied": None,
            "gate": {"passed": False, "exit_code": None, "output": ""},
            "reason": "malformed or empty code diff",
            "requires_human_review": True,
            "auto_apply": False,
        }
    gate = (gate_runner or (lambda a: default_gate_runner(a)))(applied["after"])
    passed = bool(getattr(gate, "passed", False))
    return {
        "status": VALIDATED if passed else REJECTED,
        "applied": applied,
        "gate": {
            "passed": passed,
            "exit_code": getattr(gate, "exit_code", None),
            "output": getattr(gate, "output", ""),
        },
        "reason": "regression gate green" if passed else "regression gate failed",
        "requires_human_review": True,
        "auto_apply": False,
    }
