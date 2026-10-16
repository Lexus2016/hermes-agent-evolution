#!/usr/bin/env python3
"""skill_audit_scan.py — standing skill-store security scan (evolution issue #153).

Implements the "scan" stage of the model -> scan -> triage -> patch chain
(documented in ../SKILL.md). Alfred-shaped: silent on success, a findings
report + nonzero exit when something needs review.

What it detects (v1):
  * NEW skills   — not present in the baseline manifest
  * CHANGED skills — a file was added, removed, or its SHA-256 changed
  * REMOVED skills — in the baseline but gone from the store
  * SUSPICIOUS content — light heuristic pass over *new/changed files only*
    (recognition is a hint for triage, never a verdict)

What it does NOT do: it never edits, deletes, or quarantines anything. The
patch stage (in the skill) proposes fixes for a human/agent to review.

Usage:
  python skill_audit_scan.py [--store PATH] [--baseline PATH]
                             [--allowlist PATH] [--out PATH] [--verbose]

Defaults (when HERMES_HOME is unset, ~/.hermes is used):
  store      HERMES_HOME/skills
  baseline   HERMES_HOME/state/skill-audit-baseline.json
  allowlist  HERMES_HOME/state/skill-audit-allowlist.json (optional)
  out        HERMES_HOME/state/skill-audit-findings.json

Exit codes:
  0  clean — no new/changed/removed/unreviewed findings; silent (no stdout)
  1  findings — review needed; findings JSON written to --out
  2  operational error (bad args, unreadable store)
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timezone, UTC
from pathlib import Path

BASELINE_VERSION = 1

# Light heuristic patterns, applied to NEW/ADDED files only. These are
# deliberately recognition-layer hints — the triage stage in SKILL.md makes
# the actual call. A match here is a LOW-severity finding, never a verdict.
PATTERNS: list[tuple[str, str, str]] = [
    (
        "pipe-to-shell",
        r"(?:curl|wget)\s+[^\n|]*\|\s*(?:ba)?sh\b",
        "network fetch piped straight to a shell",
    ),
    (
        "decode-then-pipe",
        r"base64\s+(?:-d|--decode)[^\n|]*\|",
        "base64 decode piped into another command",
    ),
    ("py-dynamic-exec", r"\b(?:eval|exec)\s*\(", "dynamic code execution (python)"),
    ("py-shell-true", r"shell\s*=\s*True", "subprocess with shell=True (python)"),
    (
        "hardcoded-secret",
        r"\b(?:sk-[A-Za-z0-9]{20,}|(?:api[_-]?key|password|passwd|secret|token)\s*[:=]\s*[\"'][A-Za-z0-9!@#$%^&*._\-]{12,}[\"'])",
        "possible hardcoded secret/token",
    ),
]
HIGH_SEVERITY = {"hardcoded-secret"}

_SKIP_DIRS = {"__pycache__", ".git", ".venv", "node_modules"}
_TEXT_EXTENSIONS = {
    ".md",
    ".py",
    ".sh",
    ".txt",
    ".json",
    ".yaml",
    ".yml",
    ".toml",
    ".cfg",
    ".ini",
    ".env",
    ".js",
    ".ts",
    ".html",
    ".css",
}


def _utcnow() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _default_paths() -> dict[str, Path]:
    hermes_home = Path(
        os.environ.get("HERMES_HOME", str(Path.home() / ".hermes"))
    ).expanduser()
    return {
        "store": hermes_home / "skills",
        "baseline": hermes_home / "state" / "skill-audit-baseline.json",
        "allowlist": hermes_home / "state" / "skill-audit-allowlist.json",
        "out": hermes_home / "state" / "skill-audit-findings.json",
    }


def discover_skills(store: Path) -> dict[str, Path]:
    """Return {skill_name: skill_dir} for every directory containing SKILL.md.

    Handles both flat stores (skill dirs directly under the store) and
    categorized stores (skills nested under a category dir, e.g.
    security/ai-safe-audit). A skill's name is its path relative to the
    store root.
    """
    skills: dict[str, Path] = {}
    for root, dirs, files in os.walk(store):
        root_path = Path(root)
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
        if "SKILL.md" in files and root_path != store:
            name = root_path.relative_to(store).as_posix()
            skills[name] = root_path
    return dict(sorted(skills.items()))


def hash_skill_files(skill_dir: Path) -> dict[str, str]:
    """Return {relative_path: sha256} for every regular file under skill_dir."""
    hashes: dict[str, str] = {}
    for root, dirs, files in os.walk(skill_dir):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
        for fname in sorted(files):
            if fname.endswith((".pyc", ".pyo")):
                continue
            path = Path(root) / fname
            rel = path.relative_to(skill_dir).as_posix()
            digest = hashlib.sha256()
            with open(path, "rb") as fh:
                for chunk in iter(lambda: fh.read(65536), b""):
                    digest.update(chunk)
            hashes[rel] = digest.hexdigest()
    return dict(sorted(hashes.items()))


def load_json(path: Path, default: object) -> object:
    if not path.exists():
        return default
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (json.JSONDecodeError, OSError):
        return default


def load_allowlist(path: Path) -> set[str]:
    data = load_json(path, None)
    if isinstance(data, list):
        return {str(x) for x in data}
    if isinstance(data, dict) and isinstance(data.get("skills"), list):
        return {str(x) for x in data["skills"]}
    return set()


def scan_suspicious(skill_dir: Path, rel_files: list[str]) -> list[dict]:
    """Heuristic pass over new/added text files. Returns finding dicts."""
    findings: list[dict] = []
    for rel in rel_files:
        path = skill_dir / rel
        if path.suffix.lower() not in _TEXT_EXTENSIONS:
            continue
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        except OSError:
            continue
        for name, pattern, reason in PATTERNS:
            if re.search(pattern, text, flags=re.IGNORECASE):
                findings.append({
                    "type": "suspicious_content",
                    "file": rel,
                    "pattern": name,
                    "reason": reason,
                    "severity": "high" if name in HIGH_SEVERITY else "low",
                    "action": "heuristic hint — triage manually; patch stage proposes, never auto-applies",
                })
    return findings


def compare_store(
    store: Path,
    baseline: dict,
    allowlist: set[str],
) -> tuple[list[dict], dict, set[str]]:
    """Diff the live store against the baseline.

    Returns (findings, next_baseline_skills, allowlisted_skipped).
    """
    live = discover_skills(store)
    live_hashes = {name: hash_skill_files(d) for name, d in live.items()}
    known = baseline.get("skills", {}) if isinstance(baseline, dict) else {}

    findings: list[dict] = []
    allowlisted_skipped: set[str] = set()

    for name, hashes in live_hashes.items():
        if name not in known:
            if name in allowlist:
                allowlisted_skipped.add(name)
            else:
                findings.append({
                    "type": "new_skill",
                    "skill": name,
                    "files": sorted(hashes),
                    "severity": "medium",
                    "action": "review the skill before first use; add to the allowlist once reviewed",
                })
                for suspicious in scan_suspicious(live[name], sorted(hashes)):
                    suspicious["skill"] = name
                    findings.append(suspicious)
            continue
        prev = known[name]
        added = sorted(set(hashes) - set(prev))
        removed = sorted(set(prev) - set(hashes))
        modified = sorted(f for f in set(hashes) & set(prev) if hashes[f] != prev[f])
        if added or removed or modified:
            findings.append({
                "type": "changed_skill",
                "skill": name,
                "changes": {
                    "added": added,
                    "modified": modified,
                    "removed": removed,
                },
                "severity": "medium",
                "action": "review the diff against the baseline — allowlisted skills are not exempt from change review",
            })
            for suspicious in scan_suspicious(live[name], added):
                suspicious["skill"] = name
                findings.append(suspicious)

    for name in known:
        if name not in live_hashes:
            findings.append({
                "type": "removed_skill",
                "skill": name,
                "severity": "low",
                "action": "confirm removal was intentional",
            })

    return findings, live_hashes, allowlisted_skipped


def write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, sort_keys=True)
        fh.write("\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    defaults = _default_paths()
    parser.add_argument(
        "--store", type=Path, default=defaults["store"], help="skill store root"
    )
    parser.add_argument(
        "--baseline",
        type=Path,
        default=defaults["baseline"],
        help="baseline manifest path",
    )
    parser.add_argument(
        "--allowlist",
        type=Path,
        default=defaults["allowlist"],
        help="reviewed-skill allowlist path",
    )
    parser.add_argument(
        "--out", type=Path, default=defaults["out"], help="findings output path"
    )
    parser.add_argument(
        "--verbose", action="store_true", help="print summary even when clean"
    )
    args = parser.parse_args(argv)

    store = args.store.expanduser().resolve()
    if not store.is_dir():
        print(
            f"skill-audit: error: store not found or not a directory: {store}",
            file=sys.stderr,
        )
        return 2

    baseline = load_json(args.baseline.expanduser(), {})
    if not isinstance(baseline, dict):
        baseline = {}
    allowlist = load_allowlist(args.allowlist.expanduser())

    # First run (or a wiped/corrupt baseline) establishes the known-good
    # state silently — there is nothing to compare against yet, so every
    # skill would otherwise look "new" and the very first scan would
    # alarm on the entire store.
    first_run = not isinstance(baseline, dict) or not baseline.get("skills")
    if first_run:
        live = discover_skills(store)
        live_hashes = {name: hash_skill_files(d) for name, d in live.items()}
        findings, allowlisted_skipped = [], set()
    else:
        findings, live_hashes, allowlisted_skipped = compare_store(
            store, baseline, allowlist
        )

    # Baseline always advances: each change surfaces exactly once, then the
    # new hashes become the known state for the next scan.
    next_baseline = {
        "version": BASELINE_VERSION,
        "store": str(store),
        "scanned_at": _utcnow(),
        "skills": live_hashes,
    }
    try:
        write_json(args.baseline.expanduser(), next_baseline)
    except OSError as exc:
        print(f"skill-audit: error: cannot write baseline: {exc}", file=sys.stderr)
        return 2

    if not findings:
        if args.verbose:
            n = len(live_hashes)
            print(f"skill-audit: clean — {n} skill(s) scanned, nothing new or changed")
        if allowlisted_skipped:
            # No alarm, but record the skipped skills for observability so a
            # nightly run leaves a trace of what it chose not to report.
            report = {
                "scan_date": _utcnow(),
                "store": str(store),
                "baseline": str(args.baseline.expanduser()),
                "summary": {
                    "new_skills": 0,
                    "changed_skills": 0,
                    "removed_skills": 0,
                    "suspicious_files": 0,
                },
                "allowlisted_skipped": sorted(allowlisted_skipped),
                "findings": [],
            }
            try:
                write_json(args.out.expanduser(), report)
            except OSError as exc:
                print(
                    f"skill-audit: error: cannot write findings: {exc}", file=sys.stderr
                )
                return 2
        return 0

    summary = {
        "new_skills": sum(1 for f in findings if f["type"] == "new_skill"),
        "changed_skills": sum(1 for f in findings if f["type"] == "changed_skill"),
        "removed_skills": sum(1 for f in findings if f["type"] == "removed_skill"),
        "suspicious_files": sum(
            1 for f in findings if f["type"] == "suspicious_content"
        ),
    }
    report = {
        "scan_date": _utcnow(),
        "store": str(store),
        "baseline": str(args.baseline.expanduser()),
        "summary": summary,
        "allowlisted_skipped": sorted(allowlisted_skipped),
        "findings": findings,
    }
    try:
        write_json(args.out.expanduser(), report)
    except OSError as exc:
        print(f"skill-audit: error: cannot write findings: {exc}", file=sys.stderr)
        return 2

    print(
        "skill-audit: "
        f"{summary['new_skills']} new, {summary['changed_skills']} changed, "
        f"{summary['removed_skills']} removed, {summary['suspicious_files']} suspicious "
        f"— findings: {args.out}"
    )
    for finding in findings:
        where = finding.get("skill", finding.get("file", "?"))
        print(f"  [{finding['severity']}] {finding['type']}: {where}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
