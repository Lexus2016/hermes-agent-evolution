#!/usr/bin/env python3
"""Misevolution guard for the self-improvement pipeline (#3191).

Deterministic read+flag audit of merged evolution work: flags guard-weakening
edits to its own safety constants and proxy-metric drift. Watchdog surfaces.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

# Pipeline safety constants bounding the pipeline's own autonomy.
GUARD_TOKENS = (
    "DEFAULT_MAX_LINES",
    "EVOLUTION_MERGE_MAX_LINES",
    "EVOLUTION_SKILL_EDIT_BUDGET_RATIO",
)

MIN_MERGE_RATE = 0.4


def guard_weakening_flags(diff_text: str) -> List[str]:
    """Return guard tokens whose definition lines were modified in a diff."""
    flags = []
    for token in GUARD_TOKENS:
        for line in diff_text.splitlines():
            if (
                token in line
                and line[:1] in ("+", "-")
                and not line.startswith(("+++", "---"))
            ):
                flags.append(token)
                break
    return flags


def volume_drift_flag(
    proposals_prev: int, proposals_curr: int, merged_curr: int
) -> Optional[str]:
    """Flag volume-vs-outcome drift between two consecutive windows."""
    if proposals_curr <= 0:
        return None
    rate = merged_curr / proposals_curr
    if rate < MIN_MERGE_RATE and proposals_curr >= proposals_prev:
        return (
            f"merge_rate {rate:.2f} < {MIN_MERGE_RATE} while proposal volume "
            f"held/grew ({proposals_prev} -> {proposals_curr}) — possible "
            f"proxy-metric optimization"
        )
    return None


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=60
    )
    if result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout


def _resolve_log_ref(repo: Path) -> str:
    """Resolve the best ref for git log, preferring origin/main if available."""
    try:
        _git(repo, "rev-parse", "--verify", "origin/main")
        return "origin/main"
    except Exception:
        return "HEAD"


def audit_merged_commits(repo: Path, count: int = 30) -> List[str]:
    """Scan recent merge commits for guard-weakening edits."""
    ref = _resolve_log_ref(repo)
    log = _git(
        repo, "log", f"-{count}", "--first-parent", "--format=%H %s", ref
    )
    flags: List[str] = []
    for line in log.splitlines():
        sha, _, subject = line.partition(" ")
        # Conservative scope: only commits plausibly touching the pipeline's
        # own scripts/config, to keep diff fetches cheap.
        if not any(
            k in subject.lower()
            for k in ("evolution", "gate", "merge", "lint", "skill")
        ):
            continue
        try:
            diff = _git(repo, "show", "--format=", sha, "--", "scripts", ".github")
        except RuntimeError:
            continue
        for token in guard_weakening_flags(diff):
            flags.append(f"{sha[:10]} ({subject.strip()}): modified {token}")
    return flags


def run_audit(repo: Path, metrics_path: Optional[Path] = None) -> Dict[str, Any]:
    """Run both checks; returns a JSON-serializable verdict dict."""
    verdict: Dict[str, Any] = {"flags": [], "ok": True}
    try:
        verdict["flags"].extend(audit_merged_commits(repo))
    except (RuntimeError, OSError) as exc:
        verdict["flags"].append(f"guard-scan unavailable: {exc}")

    if metrics_path is not None and metrics_path.exists():
        try:
            data = json.loads(metrics_path.read_text(encoding="utf-8"))
            windows = data.get("windows", [])
            if len(windows) >= 2:
                prev, curr = windows[-2], windows[-1]
                drift = volume_drift_flag(
                    int(prev.get("proposals", 0)),
                    int(curr.get("proposals", 0)),
                    int(curr.get("merged", 0)),
                )
                if drift:
                    verdict["flags"].append(drift)
        except (OSError, ValueError, KeyError) as exc:
            verdict["flags"].append(f"metrics unreadable: {exc}")

    verdict["ok"] = not verdict["flags"]
    return verdict


def main(argv: List[str]) -> int:
    repo = Path(argv[1]) if len(argv) > 1 else Path.cwd()
    metrics = Path(argv[2]) if len(argv) > 2 else None
    print(json.dumps(run_audit(repo, metrics), indent=2))
    return 0  # read+flag only: never a hard failure; watchdog reads the JSON.


if __name__ == "__main__":
    sys.exit(main(sys.argv))
