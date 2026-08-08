#!/usr/bin/env python3
"""Evaluator isolation check for BenchJack defense (#1267, Slice 1).

Deterministic assertion that merge-verification runs separately from the
implementing subagent (BenchJack, arXiv:2605.12673). Pure string scan.
Usage: python3 scripts/evolution_isolation_check.py [--strict] [--scripts ...]
"""

from __future__ import annotations

import argparse
import re
from dataclasses import dataclass, field
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent

PIPELINE_SCRIPTS = (
    "evolution_merge_gate.py",
    "evolution_rubric_judge.py",
    "evolution_evaluator.py",
    "evolution_qc_review.py",
    "eval_baseline.py",
)

# (regex, id, description) — evaluator/implementer coupling indicators.
COUPLING_PATTERNS = (
    (
        r"(?:def\s+\w*merge\w*|def\s+\w*verif\w*|def\s+\w*gate\w*).*\n"
        r"(?:\s+).*AIAgent\s*\(",
        "gate-instantiates-agent",
        "Gate directly instantiates AIAgent — verifier should be separate.",
    ),
    (
        r"(?:def\s+\w*judg\w*|def\s+\w*eval\w*|def\s+\w*score\w*).*\n"
        r"(?:.*\n){0,5}.*"
        r"conversation_history\s*\.\s*(?:append|extend|insert)",
        "judge-mutates-conversation",
        "Judge mutates implementer's conversation_history — no shared state.",
    ),
    (
        r"from\s+(?:eval_runner|eval_baseline)\s+import\s+\w*(?:run|main)\w*",
        "gate-imports-runner",
        "Gate imports eval runner — verify in separate invocation.",
    ),
)


@dataclass
class Violation:
    script: str
    line: int
    pattern_id: str
    description: str
    context: str


@dataclass
class IsolationReport:
    violations: list = field(default_factory=list)
    scripts_scanned: list = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not self.violations


def scan_script(name, source):
    """Scan one script for coupling patterns using a sliding-line window."""
    if not source:
        return []
    violations = []
    lines = source.splitlines(keepends=True)
    for i in range(len(lines)):
        chunk = "".join(lines[i : i + 8])
        for pattern, pid, desc in COUPLING_PATTERNS:
            if re.search(pattern, chunk, re.MULTILINE) and not any(
                v.script == name and v.pattern_id == pid and v.line == i + 1
                for v in violations
            ):
                violations.append(
                    Violation(name, i + 1, pid, desc, lines[i].rstrip()[:120])
                )
    return violations


def run_isolation_check(scripts=None):
    """Run isolation check over pipeline scripts."""
    report = IsolationReport()
    for name in list(scripts or PIPELINE_SCRIPTS):
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
    """Format the report as a human-readable string."""
    out = [
        f"Evaluator isolation check — {len(report.scripts_scanned)} script(s) scanned",
        f"Scripts: {', '.join(report.scripts_scanned)}",
        "",
    ]
    if report.passed:
        out.append("✓ PASS — no evaluator/implementer coupling detected.")
    else:
        out.append(f"✗ FAIL — {len(report.violations)} coupling point(s) detected:")
        for v in report.violations:
            out += [
                "",
                f"  [{v.pattern_id}] {v.script}:{v.line}",
                f"    {v.description}",
                f"    Context: {v.context}",
            ]
    return "\n".join(out)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--strict", action="store_true")
    ap.add_argument("--scripts", nargs="*", default=None)
    args = ap.parse_args(argv)
    report = run_isolation_check(args.scripts)
    print(format_report(report))
    return args.strict and not report.passed

if __name__ == "__main__":
    raise SystemExit(int(main()))
