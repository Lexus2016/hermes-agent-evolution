#!/usr/bin/env python3
"""LLM-judge prompt safety scan for BenchJack defense (#1267, Slice 2).

Static scan for un-delimited agent-content interpolation in LLM-judge prompts.
CAR-bench failure mode: agent output interpolated into judge prompts as
instructions rather than data. Identifies unsafe interpolation patterns.
Usage: python3 scripts/evolution_judge_safety_scan.py [--strict]
"""

from __future__ import annotations

import argparse
import re
from dataclasses import dataclass, field
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent

JUDGE_SCRIPTS = (
    "evolution_rubric_judge.py",
    "evolution_qc_review.py",
    "evolution_evaluator.py",
)

# Agent-content variable names dangerous if interpolated without delimiters.
AGENT_CONTENT_VARS = (
    "pr_body",
    "implementation",
    "impl_summary",
    "test_output",
    "agent_output",
    "response",
    "answer",
    "result",
    "summary",
    "trajectory",
    "narrative",
)

# Delimiters that indicate SAFE interpolation (content is wrapped).
SAFE_DELIMITERS = (r"<data>.*?</data>", r"<untrusted>.*?</untrusted>", r"```")


@dataclass
class SafetyViolation:
    script: str
    line: int
    snippet: str
    issue: str


@dataclass
class SafetyReport:
    violations: list = field(default_factory=list)
    scripts_scanned: list = field(default_factory=list)

    @property
    def passed(self):
        return not self.violations


def scan_script(name, source):
    """Scan for unsafe agent-content interpolation in judge prompts."""
    if not source:
        return []
    violations = []
    lines = source.splitlines(keepends=True)
    for i in range(len(lines)):
        ctx = "".join(lines[max(0, i - 5) : i + 5]).lower()
        if not any(
            kw in ctx for kw in ("prompt", "judge", "evaluate", "assess", "rubric")
        ):
            continue
        line_text = lines[i]
        for var in AGENT_CONTENT_VARS:
            if "{" + var in line_text:
                if any(re.search(p, line_text) for p in SAFE_DELIMITERS):
                    continue
                if not any(v.line == i + 1 for v in violations):
                    violations.append(
                        SafetyViolation(
                            name,
                            i + 1,
                            line_text.rstrip()[:100],
                            f"Agent content '{var}' interpolated without data delimiters.",
                        )
                    )
    return violations


def run_safety_scan(scripts=None):
    """Run the safety scan over judge template scripts."""
    report = SafetyReport()
    for name in list(scripts or JUDGE_SCRIPTS):
        path = SCRIPTS_DIR / name
        if not path.exists():
            continue
        try:
            source = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        report.scripts_scanned.append(name)
        report.violations.extend(scan_script(name, source))
    return report


def format_report(report):
    out = [
        f"Judge safety scan — {len(report.scripts_scanned)} script(s) scanned",
        f"Scripts: {', '.join(report.scripts_scanned)}",
        "",
    ]
    if report.passed:
        out.append("✓ PASS — no unsafe agent-content interpolation found.")
    else:
        out.append(f"✗ FAIL — {len(report.violations)} unsafe interpolation(s) found:")
        for v in report.violations:
            out += [
                "",
                f"  {v.script}:{v.line}",
                f"    {v.issue}",
                f"    Snippet: {v.snippet}",
            ]
    return "\n".join(out)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--strict", action="store_true")
    ap.add_argument("--scripts", nargs="*", default=None)
    args = ap.parse_args(argv)
    report = run_safety_scan(args.scripts)
    print(format_report(report))
    return args.strict and not report.passed


if __name__ == "__main__":
    raise SystemExit(int(main()))
