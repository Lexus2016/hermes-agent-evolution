#!/usr/bin/env python3
"""Deterministic policy gate for the autonomous evolution self-merge step.

The integration stage self-merges its OWN ``evolution/issue-*`` PRs into main and
then auto-deploys — a bad self-merge lands on every install. Branch protection
already requires green CI; this adds the OPERATIONAL-SAFETY policy the agent
cannot be trusted to enforce on itself, plus an ATOMIC merge that closes the
check-then-merge (TOCTOU) race the prompt-level branch-integrity check has.

Blocking policy (all deterministic, zero-false-positive — a violation means the
change genuinely needs a human, not that the heuristic guessed):

* ``DIFF_TOO_LARGE`` — total changed lines exceed the cap. A large autonomous
  change is the agent spiraling or a major refactor that warrants human review.
* ``HIGH_RISK_PATH`` — the PR touches infrastructure the agent must never rewrite
  unattended: CI/CD workflows, dependency lockfiles + manifests (supply-chain),
  container/infra definitions, secrets, or its OWN enforcement machinery (the
  approval policy, this gate, the cron registrar).

When the policy passes, the ``--merge`` mode merges ATOMICALLY: the GitHub merge
API is handed the reviewed head SHA, so a push that lands between check and merge
returns 409 and aborts instead of merging unreviewed code.

Pure ``check_merge_policy`` + explicit IO so the policy is import-safe and
unit-testable; the gh/network calls live only in the CLI shell.
"""

from __future__ import annotations

import fnmatch
import json
import os
import subprocess
import sys
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

# Default cap on total changed lines (additions + deletions) for an unattended
# self-merge. Overridable via EVOLUTION_MERGE_MAX_LINES.
DEFAULT_MAX_LINES = 200

# Globs (matched against the repo-relative path, case-insensitive) the agent must
# not modify in a self-merged PR. Kept conservative and specific so a normal
# code/test/docs PR never trips it.
HIGH_RISK_GLOBS: Tuple[str, ...] = (
    # CI/CD — never let the agent rewrite the runners that gate it.
    ".github/workflows/*",
    ".github/actions/*",
    # Dependency manifests + lockfiles — supply-chain / no unattended upgrades.
    "pyproject.toml",
    "uv.lock",
    "poetry.lock",
    "requirements*.txt",
    "constraints*.txt",
    "package.json",
    "package-lock.json",
    "pnpm-lock.yaml",
    "yarn.lock",
    "Cargo.lock",
    "go.mod",
    "go.sum",
    "flake.nix",
    "flake.lock",
    # Container / infra definitions.
    "Dockerfile*",
    "docker-compose*.yml",
    # Secrets / credentials — must never appear in an autonomous PR.
    ".env*",
    "**/.env*",
    "*.pem",
    "*.key",
    # The agent's OWN enforcement + deploy machinery.
    "tools/approval.py",
    "scripts/evolution_merge_gate.py",
    "scripts/register_evolution_cron.py",
)


def _norm(path: Any) -> str:
    p = str(path or "").strip()
    if p.startswith("./"):  # only the relative-path marker — keep .github/.env dots
        p = p[2:]
    return p.lower()


def check_merge_policy(
    files: Sequence[Dict[str, Any]],
    max_lines: int = DEFAULT_MAX_LINES,
    high_risk_globs: Sequence[str] = HIGH_RISK_GLOBS,
) -> List[str]:
    """Return blocking-violation strings (empty == may self-merge).

    ``files`` is the ``gh pr view --json files`` shape:
    ``[{"path": str, "additions": int, "deletions": int}, ...]``.
    """
    if not isinstance(files, (list, tuple)):
        return []
    out: List[str] = []

    total = 0
    risky: List[str] = []
    globs = [g.lower() for g in high_risk_globs]
    for f in files:
        if not isinstance(f, dict):
            continue
        try:
            total += int(f.get("additions") or 0) + int(f.get("deletions") or 0)
        except (TypeError, ValueError):
            pass
        path = _norm(f.get("path"))
        if not path:
            continue
        base = path.rsplit("/", 1)[-1]
        for g in globs:
            # Match against the full path AND the basename so a glob like
            # "uv.lock" catches it at any depth, while ".github/workflows/*"
            # matches the rooted path.
            if fnmatch.fnmatch(path, g) or fnmatch.fnmatch(base, g):
                risky.append(path)
                break

    if max_lines and total > max_lines:
        out.append(
            f"DIFF_TOO_LARGE: {total} changed lines exceed the {max_lines}-line "
            f"self-merge cap — a change this size needs human review"
        )
    if risky:
        shown = ", ".join(sorted(set(risky))[:5])
        out.append(
            f"HIGH_RISK_PATH: touches infrastructure the agent must not self-merge "
            f"unattended ({shown}) — needs human review"
        )
    return out


def _run(cmd: List[str]) -> Tuple[int, str, str]:
    p = subprocess.run(cmd, capture_output=True, text=True)
    return p.returncode, p.stdout, p.stderr


def _pr_files(pr: int, repo: Optional[str], runner: Callable[[List[str]], Tuple[int, str, str]]) -> Optional[List[Dict[str, Any]]]:
    cmd = ["gh", "pr", "view", str(pr), "--json", "files"]
    if repo:
        cmd += ["--repo", repo]
    code, out, _ = runner(cmd)
    if code != 0:
        return None
    try:
        return json.loads(out).get("files") or []
    except ValueError:
        return None


def _pr_head_sha(pr: int, repo: Optional[str], runner: Callable[[List[str]], Tuple[int, str, str]]) -> Optional[str]:
    cmd = ["gh", "pr", "view", str(pr), "--json", "headRefOid"]
    if repo:
        cmd += ["--repo", repo]
    code, out, _ = runner(cmd)
    if code != 0:
        return None
    try:
        return json.loads(out).get("headRefOid")
    except ValueError:
        return None


def main(argv: List[str]) -> int:
    args = argv[1:]
    if "--pr" not in args:
        print("usage: evolution_merge_gate.py --pr N [--merge] [--method squash] [--repo O/R]")
        return 2
    pr = int(args[args.index("--pr") + 1])
    repo = args[args.index("--repo") + 1] if "--repo" in args else os.environ.get("EVOLUTION_REPO_SLUG")
    method = args[args.index("--method") + 1] if "--method" in args else "squash"
    do_merge = "--merge" in args
    try:
        max_lines = int(os.environ.get("EVOLUTION_MERGE_MAX_LINES", DEFAULT_MAX_LINES))
    except ValueError:
        max_lines = DEFAULT_MAX_LINES

    runner = _run
    files = _pr_files(pr, repo, runner)
    if files is None:
        print(f"[merge-gate] could not read PR #{pr} files (gh error) — refusing to merge")
        return 1

    violations = check_merge_policy(files, max_lines=max_lines)
    if violations:
        print(f"[merge-gate] PR #{pr} BLOCKED from autonomous self-merge:")
        for v in violations:
            print(f"  • {v}")
        return 1

    print(f"[merge-gate] PR #{pr} policy OK ({len(files)} files)")
    if not do_merge:
        return 0

    head = _pr_head_sha(pr, repo, runner)
    if not head:
        print(f"[merge-gate] could not resolve PR #{pr} head SHA — refusing to merge")
        return 1
    # Atomic merge: pass the reviewed head SHA so a concurrent push between the
    # policy check and the merge fails with 409 instead of landing unreviewed.
    slug = repo or os.environ.get("EVOLUTION_REPO_SLUG", "")
    api = f"repos/{slug}/pulls/{pr}/merge"
    code, out, err = runner(
        ["gh", "api", "--method", "PUT", api, "-f", f"sha={head}", "-f", f"merge_method={method}"]
    )
    if code != 0:
        print(f"[merge-gate] atomic merge of PR #{pr} FAILED (head moved or merge error): {err.strip().splitlines()[0] if err.strip() else 'see gh output'}")
        return 1
    print(f"[merge-gate] PR #{pr} merged atomically at {head[:9]} ({method})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
