#!/usr/bin/env python3
"""Closed-PR postmortem miner — issue #578.

Periodically scans recently closed/merged/rejected PRs from the evolution
fork and extracts durable rules/checklists for the analysis stage.  The rules
live in a dedicated JSON file that the analysis stage reads alongside other
signals, so past failure modes become persistent guard-rails for future
implementation runs.

Design points
-------------
* Pure data extraction — NO LLM, no GitHub API token beyond what ``gh``
  already has in the environment.
* Rule extraction is deterministic: it classifies the close reason from PR
  labels + closing metadata, then produces one structured rule per
  non-merged, non-duplicate closure.
* Output is idempotent on rule identity (``id``), so re-scans never create
  duplicates of a rule already persisted.  Merged PRs are tracked only via
  ``last_scanned_pr`` and stats; they never generate rules.
* The analysis stage reads ``postmortem-rules.json`` as optional input.

Output: ``<evolution_dir>/postmortem-rules.json`` following the schema in
the issue specification.

Usage
-----
    python scripts/evolution_postmortem_miner.py [--dry-run] [--limit N]

Environment
-----------
    EVOLUTION_PROFILE_DIR   overrides default output directory
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_DEFAULT_REPO = "Lexus2016/hermes-agent-evolution"
_DEFAULT_LIMIT = 20
_DEFAULT_OUTPUT_DIR = Path.home() / ".hermes" / "evolution"
_OUTPUT_FILE = "postmortem-rules.json"

# PR numbers that are always skipped (no-op / test PRs, etc.)
_SKIP_PRS: set[int] = set()

# ---------------------------------------------------------------------------
# Close-reason classification
# ---------------------------------------------------------------------------

_CLOSE_REASON_MAP: Dict[str, str] = {
    "merged": "merged",
    "implemented-on-main": "implemented-on-main",
    "rejected": "rejected",
    "needs-work": "needs-work",
    "duplicate": "duplicate",
}

# Labels that indicate a PR was actually merged (gh pr view --state closed
# may still show closed+label for merged PRs)
_MERGED_LABELS = {"merged"}


def _classify_close_reason(labels: List[str], state: str) -> str:
    """Classify a closed PR's reason from its labels and state.

    Priority order: merged > implemented-on-main > needs-work > duplicate > rejected.
    """
    label_set = {lbl.lower() for lbl in labels}

    # gh pr list --state closed returns merged PRs too; check labels first
    if _MERGED_LABELS & label_set or state.lower() == "merged":
        return "merged"
    if "implemented-on-main" in label_set:
        return "implemented-on-main"
    if "duplicate" in label_set:
        return "duplicate"
    if "needs-work" in label_set:
        return "needs-work"
    if "rejected" in label_set:
        return "rejected"
    # Fallback: if closed without merge, treat as rejected
    return "rejected"


# ---------------------------------------------------------------------------
# PR fetching
# ---------------------------------------------------------------------------


def _run_gh(args: List[str], timeout: int = 60) -> subprocess.CompletedProcess:
    """Run a ``gh`` command and return the completed process."""
    return subprocess.run(
        ["gh"] + args,
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout,
    )


def fetch_closed_prs(
    repo: str = _DEFAULT_REPO, limit: int = _DEFAULT_LIMIT
) -> List[Dict[str, Any]]:
    """Fetch recently closed PRs via ``gh pr list``.

    Returns a list of dicts with fields: number, title, state, labels, mergedAt.
    Uses ``--json`` for structured output.
    """
    result = _run_gh(
        [
            "pr",
            "list",
            "--repo",
            repo,
            "--state",
            "closed",
            "--limit",
            str(limit),
            "--json",
            "number,title,state,labels,closedAt,mergedAt",
        ]
    )
    if result.returncode != 0:
        print(
            f"[postmortem-miner] gh pr list failed: {result.stderr.strip()}",
            file=sys.stderr,
        )
        return []

    try:
        prs = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        print(
            f"[postmortem-miner] could not parse gh output: {exc}",
            file=sys.stderr,
        )
        return []

    return prs


def fetch_pr_details(
    pr_number: int, repo: str = _DEFAULT_REPO
) -> Optional[Dict[str, Any]]:
    """Fetch a single PR's closing comment and metadata.

    Returns None if the PR cannot be fetched.
    """
    result = _run_gh(
        [
            "pr",
            "view",
            str(pr_number),
            "--repo",
            repo,
            "--json",
            "number,title,state,labels,closedAt,mergedAt,body,comments",
        ]
    )
    if result.returncode != 0:
        return None
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return None


# ---------------------------------------------------------------------------
# Rule extraction
# ---------------------------------------------------------------------------


def _extract_pattern(pr: Dict[str, Any]) -> str:
    """Extract a human-readable pattern description from a PR's title + labels.

    This is a deterministic heuristic — we use the PR title as the base and
    prefix with the label category when available.
    """
    title = (pr.get("title") or "").strip()
    labels = [lbl.get("name", "") for lbl in (pr.get("labels") or [])]

    # Build a category prefix from labels
    categories = [
        lbl
        for lbl in labels
        if lbl.lower() in ("fix", "bug", "improvement", "capability", "ux", "introspection")
    ]

    if categories:
        prefix = f"[{categories[0]}]"
    else:
        prefix = ""

    # If the title already has a label-style prefix, don't double up
    if prefix and title.lower().startswith(f"[{categories[0].lower()}]"):
        prefix = ""

    pattern = f"{prefix} {title}".strip() if prefix else title
    return pattern


def _make_rule_id(pr_number: int, seq: int) -> str:
    """Generate a unique rule ID from a PR number and a per-PR sequence."""
    today = datetime.now(timezone.utc).strftime("%Y%m%d")
    return f"rule-{today}-{pr_number:04d}-{seq:03d}"


def extract_rules_from_pr(
    pr: Dict[str, Any], existing_ids: set[str]
) -> List[Dict[str, Any]]:
    """Extract durable rules from a single closed PR.

    Only generates rules for non-merged, non-duplicate PRs.  Deduplicates
    against existing rule IDs.
    """
    pr_number = pr.get("number") or 0
    labels_raw = [lbl.get("name", "") for lbl in (pr.get("labels") or [])]
    state = pr.get("state", "closed")
    close_reason = _classify_close_reason(labels_raw, state)

    # Merged and duplicate PRs don't produce rules
    if close_reason in ("merged", "duplicate"):
        return []

    # Skip PRs in the exclusion set
    if pr_number in _SKIP_PRS:
        return []

    pattern = _extract_pattern(pr)
    if not pattern:
        return []

    # For each non-merged PR we produce exactly one rule.  If the rule ID
    # already exists, skip it.
    rule_id = _make_rule_id(pr_number, 1)
    if rule_id in existing_ids:
        return []

    rule = {
        "id": rule_id,
        "pattern": f"check: {pattern}",
        "source_pr": pr_number,
        "close_reason": close_reason,
        "created": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "hit_count": 0,
    }
    return [rule]


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def _output_dir() -> Path:
    """Return the output directory, respecting EVOLUTION_PROFILE_DIR."""
    env = os.environ.get("EVOLUTION_PROFILE_DIR")
    if env:
        return Path(env)
    return _DEFAULT_OUTPUT_DIR


def load_existing_rules(filepath: Path) -> Dict[str, Any]:
    """Load existing postmortem rules file, or return a fresh skeleton."""
    if not filepath.is_file():
        return {"rules": [], "last_scanned_pr": 0, "stats": {}}
    try:
        with open(filepath, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (json.JSONDecodeError, OSError):
        return {"rules": [], "last_scanned_pr": 0, "stats": {}}


def merge_rules(
    existing: Dict[str, Any],
    new_rules: List[Dict[str, Any]],
    last_scanned_pr: int,
    total_scanned: int,
    skipped_merged: int,
) -> Dict[str, Any]:
    """Merge new rules into the existing rule set, deduplicating by ID.

    Returns the full output structure.
    """
    existing_ids = {r["id"] for r in existing.get("rules", [])}
    merged_rules = list(existing.get("rules", []))

    added = 0
    for rule in new_rules:
        if rule["id"] not in existing_ids:
            merged_rules.append(rule)
            existing_ids.add(rule["id"])
            added += 1

    # Keep rules sorted by creation date, newest first
    merged_rules.sort(key=lambda r: r.get("created", ""), reverse=True)

    return {
        "rules": merged_rules,
        "last_scanned_pr": last_scanned_pr,
        "stats": {
            "total_scanned": total_scanned,
            "new_rules": added,
            "skipped_merged": skipped_merged,
            "total_rules": len(merged_rules),
        },
    }


def write_rules(filepath: Path, data: Dict[str, Any]) -> None:
    """Write the rules JSON file atomically."""
    filepath.parent.mkdir(parents=True, exist_ok=True)
    # Write to temp file then rename for atomicity
    tmp = filepath.with_suffix(filepath.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(filepath)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main(argv: List[str]) -> int:
    dry_run = "--dry-run" in argv
    limit = _DEFAULT_LIMIT
    for a in argv[1:]:
        if a.startswith("--limit="):
            try:
                limit = int(a.split("=", 1)[1])
            except ValueError:
                pass

    output_dir = _output_dir()
    output_path = output_dir / _OUTPUT_FILE

    # 1. Load existing rules to seed dedup
    existing = load_existing_rules(output_path)
    existing_ids = {r["id"] for r in existing.get("rules", [])}

    # 2. Fetch recently closed PRs
    prs = fetch_closed_prs(repo=_DEFAULT_REPO, limit=limit)
    if not prs:
        print("[postmortem-miner] no PRs fetched; exiting cleanly", file=sys.stderr)
        return 0

    # 3. Classify and extract rules
    all_new_rules: List[Dict[str, Any]] = []
    skipped_merged = 0
    last_scanned_pr = existing.get("last_scanned_pr", 0)

    for pr in prs:
        pr_number = pr.get("number") or 0
        labels_raw = [lbl.get("name", "") for lbl in (pr.get("labels") or [])]
        state = pr.get("state", "closed")

        # Update last_scanned_pr for tracking
        if pr_number > last_scanned_pr:
            last_scanned_pr = pr_number

        reason = _classify_close_reason(labels_raw, state)
        if reason == "merged":
            skipped_merged += 1
            continue

        rules = extract_rules_from_pr(pr, existing_ids)
        all_new_rules.extend(rules)

    # 4. Merge and persist
    output = merge_rules(
        existing=existing,
        new_rules=all_new_rules,
        last_scanned_pr=last_scanned_pr,
        total_scanned=len(prs),
        skipped_merged=skipped_merged,
    )

    if dry_run:
        print(json.dumps(output, indent=2, sort_keys=True))
        print(
            f"[postmortem-miner] dry-run: would write {output['stats']['new_rules']} "
            f"new rule(s) to {output_path}",
            file=sys.stderr,
        )
        return 0

    write_rules(output_path, output)
    print(
        f"[postmortem-miner] wrote {output['stats']['new_rules']} new rule(s) "
        f"(total: {output['stats']['total_rules']}) to {output_path}",
        file=sys.stderr,
    )

    # Also print consolidated JSON to stdout for the scheduler
    print(json.dumps(output, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
