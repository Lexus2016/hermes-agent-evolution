#!/usr/bin/env python3
"""Decomposition gate — block implementation of large issues that have no children.

The evolution pipeline selects issues for implementation, but an issue with
effort_score >= 0.4 that has NO child issues (decomposition slices) is too large
to land in one cycle.  This gate checks whether child issues exist for a parent,
and if not, blocks the issue with a ``blocked: needs decomposition`` verdict.

Strategy for finding children: ``gh issue list --search "repo:Lexus2016/hermes-agent-evolution mentions:#N"``
to find any open issue whose body (or comments) references the parent issue by
number (``#N``).  A child issue is expected to say something like
"parent: #579" or "implement slice of #579".

This gate is an *implementation* pipeline gate — it is called BEFORE the
implementation skill branches and starts writing code.  It is distinct from the
*analysis* decomposition gate (SKILL.md step 6b), which is a selection-time
policy recommending decomposition; this gate *enforces* it at implementation
time, blocking monolithic picks that reached the selection list without being
split.

CLI (called by evolution-implementation SKILL.md from its terminal toolset):

    evolution_decomposition_gate.py check 579 --effort 0.5   # fails (no children)
    evolution_decomposition_gate.py check 579 --effort 0.3   # passes (effort < 0.4)

Exit codes:
  0 — PASS (effort < 0.4, or effort >= 0.4 with children found)
  1 — BLOCKED (effort >= 0.4 and no child issues found)
  2 — usage / argument error
"""

from __future__ import annotations

import json
import subprocess
import sys
from typing import Any, Callable, Dict, List, Optional, Tuple

_REPO = "Lexus2016/hermes-agent-evolution"

# Issues with effort_score >= this threshold must have child issues.
EFFORT_THRESHOLD = 0.4


def _default_runner(cmd: List[str]) -> Tuple[int, str, str]:
    """Execute a command, returning (returncode, stdout, stderr)."""
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    return proc.returncode, (proc.stdout or ""), (proc.stderr or "")


def find_child_issues(
    parent_number: int,
    *,
    repo: str = _REPO,
    runner: Callable[[List[str]], Tuple[int, str, str]] | None = None,
) -> List[Dict[str, Any]] | None:
    """Find open issues that reference ``parent_number`` (e.g. via ``#N`` in body/comments).

    Uses ``gh issue list --search`` to find mentions.  Returns a list of issue
    dicts (``{number, title}``) on success, ``None`` on gh failure
    (fail-OPEN — never block on a transient API error).
    """
    runner = runner or _default_runner
    # Search for open issues that mention the parent issue number.
    search_query = f"repo:{repo} is:issue is:open mentions:#{parent_number}"
    rc, stdout, _stderr = runner([
        "gh",
        "issue",
        "list",
        "--repo",
        repo,
        "--search",
        search_query,
        "--state",
        "open",
        "--limit",
        "50",
        "--json",
        "number,title",
    ])
    if rc != 0:
        return None
    try:
        data = json.loads(stdout)
        if not isinstance(data, list):
            return None
    except (ValueError, TypeError):
        return None

    # Filter out the parent itself (it mentions its own number).
    children = [
        it for it in data if isinstance(it, dict) and it.get("number") != parent_number
    ]
    return children


def check_decomposition_gate(
    issue_number: int,
    effort_score: float,
    *,
    repo: str = _REPO,
    runner: Callable[[List[str]], Tuple[int, str, str]] | None = None,
) -> Dict[str, Any]:
    """Check whether an issue passes the decomposition gate.

    Args:
        issue_number: The GitHub issue number.
        effort_score: The estimated effort (0.0–1.0) from the analysis stage.
        repo: The GitHub repository (default: Lexus2016/hermes-agent-evolution).
        runner: Injectable subprocess runner for testing.

    Returns a dict:
        {
            "issue_number": int,
            "effort_score": float,
            "passed": bool,
            "reason": str,
            "child_count": int | None,  # None when gh was unavailable
        }
    """
    if effort_score < EFFORT_THRESHOLD:
        return {
            "issue_number": issue_number,
            "effort_score": effort_score,
            "passed": True,
            "reason": f"effort {effort_score} < {EFFORT_THRESHOLD} — no decomposition required",
            "child_count": None,
        }

    children = find_child_issues(issue_number, repo=repo, runner=runner)

    if children is None:
        # gh unavailable — fail-OPEN (pass).  Never block implementation on a
        # transient API error; wrongfully blocking is worse than letting a large
        # issue through (the implementation skill already has a size-awareness
        # clause in step 2a of its SKILL.md).
        return {
            "issue_number": issue_number,
            "effort_score": effort_score,
            "passed": True,
            "reason": "gh search unavailable; failing open — do not block on transient errors",
            "child_count": None,
        }

    child_count = len(children)

    if child_count > 0:
        child_list = ", ".join(f"#{c['number']}" for c in children[:5])
        if child_count > 5:
            child_list += f", … ({child_count} total)"
        return {
            "issue_number": issue_number,
            "effort_score": effort_score,
            "passed": True,
            "reason": f"found {child_count} child issue(s): {child_list}",
            "child_count": child_count,
            "children": children,
        }

    return {
        "issue_number": issue_number,
        "effort_score": effort_score,
        "passed": False,
        "reason": (
            f"blocked: needs decomposition — effort {effort_score} >= {EFFORT_THRESHOLD} "
            f"but no child issues found referencing #{issue_number}"
        ),
        "child_count": 0,
    }


def main(argv: List[str] | None = None) -> int:
    """CLI entry point.

    Usage: evolution_decomposition_gate.py check <issue_number> --effort <float> [--repo R]

    Exit codes: 0 = pass, 1 = blocked, 2 = usage error.
    """
    if argv is None:
        argv = sys.argv

    args = list(argv[1:])
    if len(args) < 2 or args[0] != "check":
        print(
            "usage: evolution_decomposition_gate.py check <issue_number> --effort <float> [--repo R]",
            file=sys.stderr,
        )
        return 2

    # Parse positional: issue_number
    try:
        issue_number = int(args[1])
    except (ValueError, IndexError):
        print(
            f"invalid issue_number: {args[1] if len(args) > 1 else 'missing'}",
            file=sys.stderr,
        )
        return 2

    effort_score: Optional[float] = None
    repo = _REPO
    i = 2
    while i < len(args):
        tok = args[i]
        if tok == "--effort":
            if i + 1 >= len(args):
                print("--effort requires a value", file=sys.stderr)
                return 2
            try:
                effort_score = float(args[i + 1])
            except ValueError:
                print(f"invalid --effort: {args[i + 1]!r}", file=sys.stderr)
                return 2
            i += 2
        elif tok == "--repo":
            if i + 1 >= len(args):
                print("--repo requires a value", file=sys.stderr)
                return 2
            repo = args[i + 1]
            i += 2
        else:
            print(f"unknown flag: {tok}", file=sys.stderr)
            return 2

    if effort_score is None:
        print("--effort is required", file=sys.stderr)
        return 2

    result = check_decomposition_gate(issue_number, effort_score, repo=repo)
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
