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

try:
    from evolution.lib.stage_gate import decide as _gate_decide
    from evolution.lib.stage_gate import record_decision as _gate_record
    from evolution.lib.stage_result import StageResult

    _HAS_STAGE_RESULT = True
except ImportError:  # pragma: no cover - exercised by the standalone-copy tests
    # This script runs standalone from the access gate and from cron, where the
    # `evolution` package is frequently not importable (the gate copies just
    # this file into a working directory; see #1304/#1314 for the same lesson in
    # the harvest cron). Triage output is the contract here — the StageResult
    # tuple is additive telemetry, so its absence must not stop the file from
    # being written.
    _HAS_STAGE_RESULT = False

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


def estimate_proposal_confidence(proposal: dict, sidecars: dict) -> int:
    """Cheap reflexive-style P(True) estimate for one proposal (#2386).

    Local triage has no LLM, so confidence is derived from observable
    evidence in the sidecars — a deliberately conservative heuristic:
    every known fact nudges the estimate up, unknowns pull it down, and
    the result is clamped to [5, 95] because a point estimate should
    never claim certainty (the trajectory-UQ lesson from the source
    paper: single-turn confidence does not transfer).
    """
    conf = 50  # no evidence either way
    if proposal.get("issue_number"):
        conf += 10  # filed as a tracked issue
    if proposal.get("impact_score", 0) > 0 and proposal.get("effort_score", 0) > 0:
        conf += 10  # both planning scores recorded
    if len(str(proposal.get("title", ""))) >= 20:
        conf += 10  # substantive (non-stub) title
    if "issues" in sidecars and "introspection" in sidecars:
        conf += 10  # cross-referenced evidence available
    if proposal.get("effort_score", 0) == 0:
        conf -= 15  # unknown effort = largest planning uncertainty
    if proposal.get("priority_score", 0) < 0.8:
        conf -= 10  # weak signal even before confidence
    return max(5, min(95, conf))


# Minimum confidence to stay in the selection; below this the proposal
# is deferred for a second research pass instead of implemented (#2386).
MIN_SELECTION_CONFIDENCE = 40


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

    # Apply effort budget cap, with confidence estimation + abstention
    # (#2386): each proposal gets a cheap reflexive-style confidence
    # estimate; proposals below MIN_SELECTION_CONFIDENCE are deferred
    # (kept for a second research pass) rather than selected blind.
    max_effort = cal["effort_budget"]
    selected = []
    deferred_low_confidence = []
    total_effort = 0.0
    for p in proposals:
        p["confidence"] = estimate_proposal_confidence(p, sidecars)
        if p["confidence"] < MIN_SELECTION_CONFIDENCE:
            deferred_low_confidence.append({
                "issue_number": p["issue_number"],
                "title": p["title"],
                "confidence": p["confidence"],
                "reason": "below-min-confidence — needs a second research pass",
            })
            continue
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
        "confidence_estimation": {
            "method": "reflexive-evidence-heuristic (#2386)",
            "min_selection_confidence": MIN_SELECTION_CONFIDENCE,
            "deferred_count": len(deferred_low_confidence),
        },
        "deferred_low_confidence": deferred_low_confidence,
    }

    # Emit a StageResult at this boundary (AREX #1338 slice A).
    #
    # The envelope is attached to the same dict it describes, so its ``result``
    # field is dropped here: keeping it would make output["stage_result"]
    # ["result"] point back at output, and json.dumps raises
    # "Circular reference detected" on exactly that. Dropping it also avoids
    # serializing the whole triage payload twice. The rest of the tuple —
    # evidence pointers, confidence, stage, timestamp — is what a consumer of
    # this boundary actually needs; the result itself is the document it is
    # attached to.
    #
    # Skipped when the evolution package is not importable (see the import
    # guard above) — the triage output is the contract, this is telemetry.
    if _HAS_STAGE_RESULT:
        evidence = list(output["sidecars_read"].values())
        stage_result = StageResult.wrap(
            result=None,
            evidence_pointers=[p for p in evidence if p],
            confidence=50,
            stage="local_triage",
            timestamp=datetime.now(timezone.utc).isoformat(),
        )
        envelope = stage_result.to_dict()
        envelope.pop("result", None)
        output["stage_result"] = envelope

        # Consume the tuple through the Accept/Refine/Restart gate (#1339).
        # Advisory at this boundary: local triage is a read-only pre-pass whose
        # output the analysis stage consumes, so the gate records which branch
        # the boundary lands in rather than aborting the pass. Confidence 50
        # (evidence present, no LLM verification) sits below the conservative
        # default of 70, so a triage run with sidecars lands in `refine` and one
        # with none lands in `restart` — both surfaced for the next stage to act
        # on instead of being silently treated as a confident result.
        decision = _gate_decide(stage_result)
        output["stage_gate"] = decision.to_dict()
        # Persist for the per-boundary rate metrics (#1340). Fire-and-forget:
        # record_decision swallows IO errors so a metrics write can never take
        # the triage pass down.
        _gate_record(evolution_dir / "stage_gate.jsonl", decision)
    return output


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        description="Local triage pass (no GitHub API calls)"
    )
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
        evolution_dir = Path(hermes_home) / "evolution"

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
            print(
                f"Skipping: full analysis already exists at {output_path}",
                file=sys.stderr,
            )
            return 0

    output_path.write_text(
        json.dumps(output, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    print(f"Local triage written to {output_path}")
    print(f"  Sidecars read: {', '.join(output['sidecars_read'].keys())}")
    print(f"  Selected: {len(output['selected_for_implementation'])} issues")
    print(f"  Effort budget: {output['effort_budget']}")
    ce = output.get("confidence_estimation")
    if ce:
        print(
            f"  Confidence gate: min={ce['min_selection_confidence']}, "
            f"deferred={ce['deferred_count']}"
        )
    gate = output.get("stage_gate")
    if gate:
        print(
            f"[stage-gate] {gate['stage']}: {gate['branch'].upper()} "
            f"(confidence={gate['confidence']}, threshold={gate['threshold']}) — {gate['reason']}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
