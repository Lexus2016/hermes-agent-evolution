#!/usr/bin/env python3
"""Local-state research fallback for evolution-research (#733).

When the live web tools (``web_search``, browser, GitHub, arXiv) are
unavailable, the research stage cannot scan the frontier and would otherwise
return an empty report, breaking the self-improvement loop on restricted
installs.

This module mines LOCAL project telemetry instead — ``metrics.jsonl``,
``funnel-summary.txt`` and prior ``research/*.md`` reports — and surfaces
deterministic, pipeline-quality findings (integration stalls, low selection
efficiency, research stagnation, stale frontier scans) mapped to the SAME
impact/effort/priority schema live research uses. No network calls.

The fallback is gated by an explicit capability check (:func:`web_tools_available`),
not a silent failure: callers switch to this path only when the live tools are
missing, and the report is never silently empty.

Usage:
    python scripts/evolution_research_local.py [--evolution-dir DIR] [--print]

Output:
    Writes ``research/YYYY-MM-DD.md`` (Markdown, live-research schema) to the
    evolution directory, unless a live (non-local) report already exists for
    today. Prints a one-line summary.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

# Live web/research tools the research stage normally needs. If NONE are
# exposed, the caller switches to the local-state fallback in this module.
_WEB_TOOLS = ("web_search", "web_extract", "browser", "arxiv", "github")

# Priority floor shared with live research (SKILL.md: Priority Score >= 0.7).
_MIN_PRIORITY = 0.7
# Live research cap (SKILL.md: max 20 proposals).
_MAX_FINDINGS = 20

# Sentinel so re-runs recognise their own output and never clobber a live report.
_LOCAL_MARKER = "<!-- evolution-research: local-state fallback -->"


def _default_evolution_dir() -> Path:
    """Resolve the evolution profile directory WITHOUT hardcoding a profile name.

    Priority (matches the rest of the evolution script family + the runtime):
      1. ``$EVOLUTION_PROFILE_DIR`` — set explicitly by the evolution cron; the
         authoritative override.
      2. ``<hermes_home>/evolution`` — where ``hermes_home`` is ``$HERMES_HOME``
         or the platform default (``~/.hermes``). HERMES_HOME already encodes the
         active profile (default -> ``~/.hermes``, named ``foo`` ->
         ``~/.hermes/profiles/foo``), so no profile segment is hardcoded — never
         the legacy ``profiles/user1`` literal.
    """
    env = os.environ.get("EVOLUTION_PROFILE_DIR", "").strip()
    if env:
        return Path(env)

    hermes_home = Path(
        os.environ.get("HERMES_HOME", "").strip() or (Path.home() / ".hermes")
    )
    return hermes_home / "evolution"


def web_tools_available(available_tools) -> bool:
    """Capability check: True if at least one live web/research tool is exposed.

    ``available_tools`` is any iterable of tool-name strings (e.g. the runtime's
    tool registry). The research stage calls this fallback only when this
    returns False — an explicit gate, never a silent empty report.
    """
    names = {str(t) for t in (available_tools or ())}
    return any(tool in names for tool in _WEB_TOOLS)


def _priority(impact: float, effort: float) -> float:
    """Canonical evolution priority: impact dampened by effort, never divided.

    ``base = impact * 2 * (1 - 0.4 * effort)`` — matches evolution-analysis.
    """
    return round(impact * 2.0 * (1.0 - 0.4 * effort), 2)


def _load_records(metrics_file: Path) -> list[dict]:
    """Read metrics.jsonl (one JSON object per line), skipping blank/malformed
    lines. Kept dependency-free so the fallback needs no runtime import from
    evolution_funnel (mirrors evolution_backlog_gate)."""
    out: list[dict] = []
    if not metrics_file.exists():
        return out
    try:
        lines = metrics_file.read_text(encoding="utf-8").splitlines()
    except OSError:
        return out
    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict):
            out.append(obj)
    return out


def _trailing_zero_streak(records: list[dict], field: str) -> int:
    """Length of the trailing run of records whose ``field`` is 0 or absent."""
    streak = 0
    for rec in reversed(records):
        if int(rec.get(field, 0) or 0) == 0:
            streak += 1
        else:
            break
    return streak


def _reject_rate(records: list[dict]) -> float:
    """rejected / (selected + rejected) over the records; 0.0 if no decisions."""
    selected = sum(int(r.get("selected", 0) or 0) for r in records)
    rejected = sum(int(r.get("rejected", 0) or 0) for r in records)
    denom = selected + rejected
    return rejected / denom if denom else 0.0


def _latest_report_age_days(research_dir: Path, now: datetime) -> int | None:
    """Age in days of the newest ``research/*.md`` report, or None if none exist."""
    if not research_dir.is_dir():
        return None
    reports = [f for f in research_dir.iterdir() if f.suffix == ".md"]
    if not reports:
        return None
    newest = max(reports, key=lambda f: f.stat().st_mtime)
    newest_mtime = datetime.fromtimestamp(newest.stat().st_mtime, tz=timezone.utc)
    return max(0, (now - newest_mtime).days)


def _finding(
    title: str,
    category: str,
    impact: float,
    effort: float,
    source: str,
    frontier: str,
    description: str,
) -> dict:
    """Build one finding in the live-research schema."""
    return {
        "title": title,
        "category": category,  # FEATURE | IMPROVEMENT | REPLACEMENT
        "impact_score": impact,
        "effort_score": effort,
        "priority_score": _priority(impact, effort),
        "source": source,
        "frontier_standing": frontier,
        "description": description,
    }


def mine_findings(
    records: list[dict],
    funnel_summary: str = "",
    latest_research_age_days: int | None = None,
) -> list[dict]:
    """Map local telemetry signals to research findings (deterministic)."""
    findings: list[dict] = []
    window = records[-14:] if len(records) >= 7 else records

    # 1. Integration stalled — trailing merged-zero streak.
    merged_streak = _trailing_zero_streak(records, "merged")
    if merged_streak >= 3:
        findings.append(
            _finding(
                "[IMPROVEMENT] Integration is stuck — audit the merge pipeline "
                "before adding more backlog",
                "IMPROVEMENT",
                0.8,
                0.4,
                f"local telemetry: metrics.jsonl merged-zero streak of {merged_streak} cycles",
                "behind",
                f"The last {merged_streak} cycles landed zero merges. Idea supply is not the "
                "bottleneck — downstream integration (analysis -> implementation -> PR -> merge) "
                "is. Audit the executor and PR-merge path (flaky CI, review gate, worktree "
                "failures) before generating more proposals.",
            )
        )

    # 2. Research stagnation — trailing issues_created-zero streak.
    created_streak = _trailing_zero_streak(records, "issues_created")
    if created_streak >= 3:
        findings.append(
            _finding(
                "[IMPROVEMENT] Research produced no proposals for several cycles "
                "— restore frontier access",
                "IMPROVEMENT",
                0.7,
                0.3,
                f"local telemetry: metrics.jsonl issues_created-zero streak of "
                f"{created_streak} cycles",
                "behind",
                f"No new proposals were filed for {created_streak} cycles. On restricted installs "
                "this usually means the live web/arXiv/GitHub tools are unavailable. Restore "
                "frontier access or schedule a catch-up scan; meanwhile this local-state fallback "
                "keeps the self-improvement loop alive.",
            )
        )

    # 3. Low selection efficiency — high reject rate.
    reject_rate = _reject_rate(window)
    if len(window) >= 3 and reject_rate >= 0.5:
        findings.append(
            _finding(
                "[IMPROVEMENT] Triage rejects most findings — raise the research evidence bar",
                "IMPROVEMENT",
                0.6,
                0.3,
                f"local telemetry: reject rate {reject_rate:.0%} over last {len(window)} cycles",
                "at-par",
                f"Triage rejected {reject_rate:.0%} of surfaced findings recently. Research is "
                "spending effort on low-conviction ideas. Raise the evidence bar: fewer, "
                "higher-frontier proposals per cycle; a popular or new trend alone is not enough.",
            )
        )
    elif "HIGH_REJECT_RATE" in funnel_summary:
        # Funnel flag reinforces the same signal when per-cycle counts are thin.
        findings.append(
            _finding(
                "[IMPROVEMENT] Triage rejects most findings — raise the research evidence bar",
                "IMPROVEMENT",
                0.6,
                0.3,
                "local telemetry: funnel-summary.txt HIGH_REJECT_RATE flag",
                "at-par",
                "The funnel signal reports a high reject rate. Raise the research evidence bar "
                "this cycle: fewer, higher-conviction findings.",
            )
        )

    # 4. Stale frontier scan — newest research report is old.
    if latest_research_age_days is not None and latest_research_age_days >= 7:
        findings.append(
            _finding(
                "[IMPROVEMENT] Frontier scan is stale — schedule a catch-up once web tools return",
                "IMPROVEMENT",
                0.5,
                0.2,
                f"local telemetry: newest research report is {latest_research_age_days} days old",
                "behind",
                f"The most recent frontier scan is {latest_research_age_days} days old. Competitor "
                "repos and arXiv move weekly; schedule a catch-up scan as soon as web tools return "
                "so the agent does not drift behind the field.",
            )
        )

    return findings


def run_local_research(evolution_dir: Path) -> dict:
    """Mine local telemetry and return a research-report dict (no network)."""
    now = datetime.now(timezone.utc)
    today = now.strftime("%Y-%m-%d")

    records = _load_records(evolution_dir / "metrics.jsonl")

    funnel_summary = ""
    funnel_file = evolution_dir / "funnel-summary.txt"
    if funnel_file.exists():
        try:
            funnel_summary = funnel_file.read_text(encoding="utf-8")
        except OSError:
            funnel_summary = ""

    age = _latest_report_age_days(evolution_dir / "research", now)

    findings = mine_findings(records, funnel_summary, age)

    # Enforce the shared priority floor, sort by priority desc, cap the list.
    findings = [f for f in findings if f["priority_score"] >= _MIN_PRIORITY]
    findings.sort(key=lambda f: f["priority_score"], reverse=True)
    findings = findings[:_MAX_FINDINGS]

    result = {
        "date": today,
        "local_research": True,
        "cycles_analyzed": len(records),
        "findings": findings,
    }
    if not findings:
        # Never a silent empty report — state the reason explicitly.
        result["note"] = (
            "No local pipeline signals crossed the priority floor; the frontier "
            "scan is deferred until web tools are restored."
        )
    return result


def render_report(result: dict) -> str:
    """Render the result dict as the live-research Markdown schema."""
    lines = [_LOCAL_MARKER, f"# Research Report - {result['date']}", ""]
    findings = result.get("findings", [])

    if not findings:
        note = result.get("note", "no local findings")
        lines.append(f"_Local-state fallback: {note}_")
        lines.append("")
        return "\n".join(lines)

    groups: dict[str, tuple[str, list[dict]]] = {
        "FEATURE": ("## New Features", []),
        "IMPROVEMENT": ("## Improvements", []),
        "REPLACEMENT": ("## Replacements", []),
    }
    for finding in findings:
        groups.get(finding["category"], groups["IMPROVEMENT"])[1].append(finding)

    for category in ("FEATURE", "IMPROVEMENT", "REPLACEMENT"):
        header, items = groups[category]
        if not items:
            continue
        lines.append(header)
        lines.append("")
        for finding in items:
            lines.append(f"### {finding['title']}")
            lines.append(f"- **Source**: {finding['source']}")
            lines.append(f"- **Frontier standing**: {finding['frontier_standing']}")
            lines.append(f"- **Impact**: {finding['impact_score']}")
            lines.append(f"- **Effort**: {finding['effort_score']}")
            lines.append(f"- **Priority Score**: {finding['priority_score']}")
            lines.append("")
            lines.append(finding["description"])
            lines.append("")

    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        description="Local-state research fallback (no web tools, no network)."
    )
    parser.add_argument(
        "--evolution-dir",
        default=None,
        help="Path to the evolution directory (default: $EVOLUTION_PROFILE_DIR, "
        "else <hermes_home>/profiles/<active_profile>/evolution)",
    )
    parser.add_argument(
        "--print",
        action="store_true",
        help="Print the Markdown report to stdout instead of writing a file",
    )
    args = parser.parse_args(argv)

    if args.evolution_dir:
        evolution_dir = Path(args.evolution_dir)
    else:
        evolution_dir = _default_evolution_dir()

    if not evolution_dir.is_dir():
        print(f"Error: evolution directory not found: {evolution_dir}", file=sys.stderr)
        return 1

    result = run_local_research(evolution_dir)
    report = render_report(result)

    if args.print:
        print(report)
        return 0

    research_dir = evolution_dir / "research"
    research_dir.mkdir(parents=True, exist_ok=True)
    out_path = research_dir / f"{result['date']}.md"

    # Don't clobber a live (non-local) report already written today.
    if out_path.exists():
        try:
            existing = out_path.read_text(encoding="utf-8")
        except OSError:
            existing = ""
        if _LOCAL_MARKER not in existing:
            print(
                f"Skipping: live research report already exists at {out_path}",
                file=sys.stderr,
            )
            return 0

    out_path.write_text(report + "\n", encoding="utf-8")
    print(f"Local research fallback written to {out_path}")
    print(f"  Cycles analyzed: {result['cycles_analyzed']}")
    print(f"  Findings: {len(result['findings'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
