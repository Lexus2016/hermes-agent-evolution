#!/usr/bin/env python3
"""Local triage pass for evolution-analysis (#783).

Reads local sidecar files (issues/, introspection/, research/) and
produces a thin-list triage JSON in the standard analysis format —
WITHOUT any GitHub API calls or private-tool dispatch.

This makes the analysis stage independently runnable: when private
tools are unavailable, the local triage still produces output so the
pipeline is not blind.

Usage:
    python scripts/evolution_local_triage.py [--evolution-dir DIR]

Output:
    Writes analysis/YYYY-MM-DD.json to the evolution directory.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

# Sidecar subdirectories to scan
_SIDECAR_DIRS = ("issues", "introspection", "research")


def _latest_file(directory: Path) -> Path | None:
    """Return the most recently modified .json or .md file in a directory."""
    if not directory.is_dir():
        return None
    candidates = sorted(
        (f for f in directory.iterdir() if f.suffix in (".json", ".md")),
        key=lambda f: f.stat().st_mtime,
        reverse=True,
    )
    return candidates[0] if candidates else None


def _read_sidecar(path: Path) -> dict:
    """Read a sidecar file (JSON or MD) and return a dict with metadata."""
    if path.suffix == ".json":
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {"path": str(path), "error": "unreadable"}
        return {"path": str(path), "data": data}
    # MD files — just note existence (research reports are free-form)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return {"path": str(path), "error": "unreadable"}
    return {"path": str(path), "char_count": len(text)}


def _extract_proposals(issues_sidecar: dict) -> list[dict]:
    """Extract filed proposals from the latest issues sidecar."""
    data = issues_sidecar.get("data", {})
    proposals = data.get("proposals", [])
    filed = []
    for p in proposals:
        if p.get("decision") == "filed" and p.get("issue"):
            filed.append({
                "issue_number": p["issue"],
                "title": p.get("title", ""),
                "priority_score": p.get("priority_score", 0.0),
                "impact_score": p.get("impact", 0.0),
                "effort_score": p.get("effort", 0.0),
                "category": p.get("category", ""),
                "selected_reason": "local-triage",
            })
    return filed


def _read_calibration(evolution_dir: Path) -> dict:
    """Read health and realized-impact sidecars for calibration."""
    cal = {"effort_budget": 3.0, "consolidation_mode": False}

    health_file = evolution_dir / "evolution-health.txt"
    if health_file.exists():
        text = health_file.read_text(encoding="utf-8")
        for token in ("effort_budget=1.5", "effort_budget=1.5"):
            if token in text:
                cal["effort_budget"] = 1.5
                break
        # Check for LOW_SELECTION_EFFICIENCY
        if "LOW_SELECTION_EFFICIENCY" in text:
            cal["effort_budget"] = 1.5

    realized_file = evolution_dir / "realized-impact.txt"
    if realized_file.exists():
        text = realized_file.read_text(encoding="utf-8")
        if "REALIZED_IMPACT_LOW" in text:
            cal["consolidation_mode"] = True

    return cal


def run_local_triage(evolution_dir: Path) -> dict:
    """Run the local triage pass and return the analysis JSON dict."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # Read latest sidecars
    sidecars = {}
    for subdir in _SIDECAR_DIRS:
        latest = _latest_file(evolution_dir / subdir)
        if latest:
            sidecars[subdir] = _read_sidecar(latest)

    # Extract proposals from issues sidecar
    proposals = []
    if "issues" in sidecars and "data" in sidecars["issues"]:
        proposals = _extract_proposals(sidecars["issues"])

    # Read calibration
    cal = _read_calibration(evolution_dir)

    # Sort by priority score (descending)
    proposals.sort(key=lambda p: p["priority_score"], reverse=True)

    # Apply effort budget cap
    max_effort = cal["effort_budget"]
    selected = []
    total_effort = 0.0
    for p in proposals:
        if total_effort + p["effort_score"] > max_effort:
            continue
        selected.append(p)
        total_effort += p["effort_score"]

    # Build output in standard analysis format
    output = {
        "date": today,
        "local_triage": True,
        "sidecars_read": {k: v.get("path", "") for k, v in sidecars.items()},
        "effort_budget": max_effort,
        "consolidation_mode": cal["consolidation_mode"],
        "rejected": [],
        "selected_for_implementation": selected,
    }
    return output


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Local triage pass (no GitHub API calls)")
    parser.add_argument(
        "--evolution-dir",
        default=None,
        help="Path to the evolution directory (default: auto-detect from HERMES_HOME)",
    )
    args = parser.parse_args(argv)

    if args.evolution_dir:
        evolution_dir = Path(args.evolution_dir)
    else:
        import os
        hermes_home = os.environ.get("HERMES_HOME", str(Path.home() / ".hermes"))
        evolution_dir = Path(hermes_home) / "profiles" / "user1" / "evolution"

    if not evolution_dir.is_dir():
        print(f"Error: evolution directory not found: {evolution_dir}", file=sys.stderr)
        return 1

    output = run_local_triage(evolution_dir)

    # Write to analysis/YYYY-MM-DD.json
    analysis_dir = evolution_dir / "analysis"
    analysis_dir.mkdir(parents=True, exist_ok=True)
    output_path = analysis_dir / f"{output['date']}.json"

    # Don't overwrite if a full (non-local) analysis already exists for today
    if output_path.exists():
        existing = json.loads(output_path.read_text(encoding="utf-8"))
        if not existing.get("local_triage", False):
            # A real analysis exists — don't clobber it with a local-only pass
            print(f"Skipping: full analysis already exists at {output_path}", file=sys.stderr)
            return 0

    output_path.write_text(json.dumps(output, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"Local triage written to {output_path}")
    print(f"  Sidecars read: {', '.join(output['sidecars_read'].keys())}")
    print(f"  Selected: {len(output['selected_for_implementation'])} issues")
    print(f"  Effort budget: {output['effort_budget']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())