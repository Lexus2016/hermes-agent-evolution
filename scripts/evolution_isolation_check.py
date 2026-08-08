#!/usr/bin/env python3
"""Evaluator isolation check (#1807) — BenchJack root-exploit defense.

BenchJack (arXiv:2605.12673): if the process that produces an artifact also
judges whether it "worked," the score can be gamed. This deterministic check
verifies that the merge-verification / QC review step in the evolution pipeline
delegates to a *separate* subagent context (role="leaf") rather than running
inline in the implementer's context. No LLM calls.

CLI: ``python scripts/evolution_isolation_check.py [--json]``
Exit 0 if isolation enforced, 1 if coupling detected.
"""

from __future__ import annotations

import json
import re
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

_HERE = Path(__file__).resolve().parent
# Scripts involved in the implement→verify flow.
ISOLATION_TARGETS = [
    "evolution_qc_review.py",
    "evolution_orchestrator.py",
    "evolution_draft_selector.py",
    "evolution_merge_gate.py",
]
# Positive: verifier delegates to a separate subagent.
_ISOLATION_RES = [
    (re.compile(r'"role"\s*:\s*"leaf"'), "role='leaf' delegation"),
    (re.compile(r"'role'\s*:\s*'leaf'"), "role='leaf' delegation"),
    (re.compile(r"delegate_task"), "delegate_task call site"),
    (re.compile(r"subagent"), "subagent reference"),
]
# Negative: implement and verify coupled in the same scope.
_COUPLING_RES = [
    (
        re.compile(r"def\s+implement.*?def\s+(?:verif|merge_check|qc)", re.S),
        "implement+verify in same module",
    ),
]


@dataclass
class Finding:
    check: str
    severity: str  # "high" (coupling) or "info" (isolation confirmed)
    file: str
    detail: str


@dataclass
class ScanResult:
    findings: List[Finding] = field(default_factory=list)
    scripts_scanned: List[str] = field(default_factory=list)

    @property
    def has_failures(self) -> bool:
        return any(f.severity == "high" for f in self.findings)

    def to_dict(self) -> Dict[str, Any]:
        return {"findings": [asdict(f) for f in self.findings], "scripts_scanned": self.scripts_scanned, "has_failures": self.has_failures}  # fmt: skip


def scan_file(filepath: str) -> List[Finding]:
    """Check one script for evaluator/implementer coupling (#1807)."""
    path = _HERE / filepath
    if not path.exists():
        return []
    source = path.read_text(encoding="utf-8")
    out: List[Finding] = []
    for pat, desc in _COUPLING_RES:
        if pat.search(source):
            out.append(Finding("evaluator-isolation", "high", filepath, f"COUPLING: {desc}"))  # fmt: skip
    for pat, desc in _ISOLATION_RES:
        if pat.search(source):
            out.append(Finding("evaluator-isolation", "info", filepath, f"Isolation signal: {desc}"))  # fmt: skip
            break  # one positive signal is enough
    return out


def run_scan() -> ScanResult:
    """Check all isolation target scripts."""
    result = ScanResult()
    for s in ISOLATION_TARGETS:
        result.scripts_scanned.append(s)
        result.findings.extend(scan_file(s))
    return result


def format_report(r: ScanResult) -> str:
    """Human-readable pass/fail report."""
    lines = ["=" * 60, f"Evaluator Isolation Check (#1807): {len(r.findings)} finding(s)", "=" * 60]  # fmt: skip
    for f in r.findings:
        tag = "✓" if f.severity == "info" else "✗"
        lines.append(f"  {tag} [{f.severity.upper()}] {f.file}: {f.detail}")  # fmt: skip
    lines.append("RESULT: FAIL — coupling detected" if r.has_failures else "RESULT: PASS — isolation enforced")  # fmt: skip
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    argv = argv or sys.argv
    result = run_scan()
    if "--json" in argv:
        print(json.dumps(result.to_dict(), indent=2, ensure_ascii=False))
    else:
        print(format_report(result))
    return 1 if result.has_failures else 0


if __name__ == "__main__":
    raise SystemExit(main())