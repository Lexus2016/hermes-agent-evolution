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

import base64
import fnmatch
import json
import os
import subprocess
import sys
from pathlib import Path
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
    # The rest of the autonomous self-modification machinery: the per-stage job
    # definitions (their prompts CARRY the safety instructions — incl. this
    # gate's own invocation and the merge limits), the orchestrator, and every
    # other deterministic gate plus the wake-gate. An unattended self-merge here
    # could weaken the loop's own guardrails on the next auto-deploy — same
    # rationale as the three files above.
    "cron/evolution/*.yaml",
    "scripts/evolution_orchestrator.py",
    "scripts/evolution_*_gate.py",
    "scripts/evolution_*_gate.sh",
    # The integration skill IS the self-merge safety procedure (it decides when
    # this gate is invoked and how a PR is merged). Editing it unattended could
    # remove the agent's own merge guardrails, so it needs human review — same
    # class as evolution_merge_gate.py. The OTHER evolution skills are NOT here:
    # the loop is designed to self-improve those (that path stays gated by
    # CODEOWNERS/branch-protection only).
    "skills/evolution/evolution-integration/SKILL.md",
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


def check_merge_policy_with_quality(
    files: Sequence[Dict[str, Any]],
    max_lines: int = DEFAULT_MAX_LINES,
    high_risk_globs: Sequence[str] = HIGH_RISK_GLOBS,
    mock_ratio_threshold: float = 0.30,
    test_contents: Optional[Dict[str, str]] = None,
) -> List[str]:
    """Extended merge policy: diff-size + high-risk + test-quality gates (#1209, #1210).

    Runs the original :func:`check_merge_policy` (diff-size cap + high-risk
    path blocklist) and then appends test-quality violations from
    :func:`evolution_test_quality_gate.check_test_quality` (mock-ratio gate
    + fabricated-reproduction detection).

    ``test_contents`` — map of test file path → full content for
    fabricated-reproduction detection.  When empty/None the fabrication check
    has nothing to scan (the mock-ratio gate, which only needs file metadata,
    still runs); ``main()`` populates it from the reviewed head SHA so the
    fabrication check is LIVE on the real merge path (#1246).
    """
    violations = check_merge_policy(
        files, max_lines=max_lines, high_risk_globs=high_risk_globs
    )
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from evolution_test_quality_gate import check_test_quality  # noqa: E402
    except ImportError:
        # Fail CLOSED (#1246): the test-quality gate is a self-merge SAFETY
        # control. If its module can't be imported we cannot verify test
        # quality — block the unattended merge rather than silently proceeding
        # unguarded. The module lives beside this one in scripts/ and is copied
        # to every profile by register_evolution_cron.py, so a failed import
        # means a broken deploy, which SHOULD stop autonomous self-merge.
        violations.append(
            "TEST_QUALITY_GATE_UNAVAILABLE: evolution_test_quality_gate could not "
            "be imported — cannot verify test quality; refusing unattended "
            "self-merge (fix the deploy or merge with human review)"
        )
        return violations
    violations.extend(
        check_test_quality(
            files,
            test_contents=test_contents,
            mock_ratio_threshold=mock_ratio_threshold,
        )
    )
    return violations


def _run(cmd: List[str]) -> Tuple[int, str, str]:
    p = subprocess.run(cmd, capture_output=True, text=True)
    return p.returncode, p.stdout, p.stderr


def _pr_snapshot(
    pr: int,
    repo: Optional[str],
    runner: Callable[[List[str]], Tuple[int, str, str]],
) -> Tuple[Optional[List[Dict[str, Any]]], Optional[str]]:
    """Fetch the PR's changed files AND head SHA in ONE ``gh`` call.

    Returning both from a single snapshot is what makes the merge atomic: the
    policy is checked against the files of exactly the commit that will be
    merged, closing the review→merge race that two separate ``gh`` reads (files
    first, head later) leave open — a push landing between them would otherwise
    be merged with a SHA whose diff was never reviewed.

    Returns ``(files, head)``. Fails CLOSED — ``(None, None)`` on a gh error, non-
    JSON output, or a response that does not carry a proper ``files`` LIST. A
    missing ``files`` key is an unreadable PR, NOT "an empty, therefore safe,
    diff": the caller refuses to merge. ``head`` is ``None`` only when the files
    were readable but the SHA was absent.
    """
    cmd = ["gh", "pr", "view", str(pr), "--json", "files,headRefOid"]
    if repo:
        cmd += ["--repo", repo]
    code, out, _ = runner(cmd)
    if code != 0:
        return None, None
    try:
        data = json.loads(out)
    except ValueError:
        return None, None
    if not isinstance(data, dict) or not isinstance(data.get("files"), list):
        return None, None
    files = data["files"]
    head = data.get("headRefOid")
    if not isinstance(head, str) or not head:
        return files, None
    return files, head


def _looks_like_test(path: str) -> bool:
    """Cheap test-file predicate (no dependency on the quality-gate module,
    which may be unavailable). Matches ``tests/`` dirs and ``test_*`` /
    ``*_test`` Python files."""
    base = path.rsplit("/", 1)[-1]
    return (
        path.startswith("tests/")
        or "/tests/" in path
        or base.startswith("test_")
        or base.endswith("_test.py")
    )


def _fetch_test_contents(
    files: Sequence[Dict[str, Any]],
    head: Optional[str],
    slug: str,
    runner: Callable[[List[str]], Tuple[int, str, str]],
) -> Dict[str, str]:
    """Fetch the FULL content of each changed test file at the reviewed head SHA
    so the fabricated-reproduction check runs on the LIVE merge path (#1246).

    Pinned to ``head`` — an immutable commit SHA from the SAME snapshot as the
    reviewed files — so this preserves the atomic review→merge invariant: the
    fabrication check reads exactly the bytes that will be merged, never a moving
    ref. Best-effort per file: a file that can't be fetched is omitted (the
    fabrication check degrades to what it could read; the mock-ratio gate needs
    no content and is unaffected).
    """
    contents: Dict[str, str] = {}
    if not head or not slug:
        return contents
    for f in files:
        path = f.get("path") if isinstance(f, dict) else None
        if not path or not _looks_like_test(path):
            continue
        code, out, _ = runner(
            ["gh", "api", f"repos/{slug}/contents/{path}?ref={head}", "--jq", ".content"]
        )
        if code != 0 or not out.strip():
            continue
        try:
            contents[path] = base64.b64decode(out).decode("utf-8", errors="replace")
        except (ValueError, UnicodeError):
            continue
    return contents


def main(argv: List[str]) -> int:
    args = argv[1:]
    if "--pr" not in args:
        print(
            "usage: evolution_merge_gate.py --pr N [--merge] [--method squash] [--repo O/R]"
        )
        return 2
    pr = int(args[args.index("--pr") + 1])
    repo = (
        args[args.index("--repo") + 1]
        if "--repo" in args
        else os.environ.get("EVOLUTION_REPO_SLUG")
    )
    method = args[args.index("--method") + 1] if "--method" in args else "squash"
    do_merge = "--merge" in args
    try:
        max_lines = int(os.environ.get("EVOLUTION_MERGE_MAX_LINES", DEFAULT_MAX_LINES))
    except ValueError:
        max_lines = DEFAULT_MAX_LINES

    runner = _run
    # One snapshot: the files we review and the SHA we merge come from the SAME
    # gh read, so the policy is checked against exactly the commit that lands.
    files, head = _pr_snapshot(pr, repo, runner)
    if files is None:
        print(f"[merge-gate] could not read PR #{pr} files (gh error / malformed response) — refusing to merge")
        return 1

    # Fetch changed test-file contents at the reviewed head SHA so the
    # fabricated-reproduction check runs on the LIVE merge path (#1246) — pinned
    # to the same immutable SHA that will merge, so no atomicity is lost.
    slug = repo or os.environ.get("EVOLUTION_REPO_SLUG", "")
    test_contents = _fetch_test_contents(files, head, slug, runner)
    violations = check_merge_policy_with_quality(
        files, max_lines=max_lines, test_contents=test_contents
    )
    if violations:
        print(f"[merge-gate] PR #{pr} BLOCKED from autonomous self-merge:")
        for v in violations:
            print(f"  • {v}")
        return 1

    print(f"[merge-gate] PR #{pr} policy OK ({len(files)} files)")
    if not do_merge:
        return 0

    if not head:
        print(f"[merge-gate] could not resolve PR #{pr} head SHA — refusing to merge")
        return 1
    # Atomic merge: the head SHA came from the SAME snapshot as the reviewed
    # files, so passing it to the merge API means a concurrent push landed since
    # the read → 409 abort, instead of merging a diff the policy never saw.
    slug = repo or os.environ.get("EVOLUTION_REPO_SLUG", "")
    api = f"repos/{slug}/pulls/{pr}/merge"
    code, out, err = runner([
        "gh",
        "api",
        "--method",
        "PUT",
        api,
        "-f",
        f"sha={head}",
        "-f",
        f"merge_method={method}",
    ])
    if code != 0:
        print(
            f"[merge-gate] atomic merge of PR #{pr} FAILED (head moved or merge error): {err.strip().splitlines()[0] if err.strip() else 'see gh output'}"
        )
        return 1
    print(f"[merge-gate] PR #{pr} merged atomically at {head[:9]} ({method})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
