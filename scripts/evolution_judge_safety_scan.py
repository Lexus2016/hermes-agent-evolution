#!/usr/bin/env python3
"""Static prompt-injection scan for LLM-judge prompts (#1808).

BenchJack/CAR-ben defense (arXiv:2605.12673): scans evolution pipeline scripts
for LLM-judge templates interpolating untrusted agent content without data
delimiters. Deterministic AST scan, no LLM calls. CLI: ``[--json]``.
"""

from __future__ import annotations

import ast
import json
import re
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

_HERE = Path(__file__).resolve().parent
PROMPT_BUILDER_SCRIPTS = [
    "evolution_qc_review.py",
    "evolution_orchestrator.py",
    "evolution_draft_selector.py",
    "evolution_rubric_judge.py",
    "evolution_evaluator.py",
]
_AGENT_PARAMS = frozenset(
    "summary subtask angle body content report output text description comment goal".split()
)
_DELIM_RE = re.compile(
    r"<(?:untrusted|data|untrusted-content|agent_output|user_content)>", re.I
)
_PROMPT_KW = frozenset(
    "review judge evaluat summary sub-task subtask finding quality-control rubric score verdict".split()  # noqa: E501
)


@dataclass
class Finding:
    check: str
    severity: str
    file: str
    line: int
    description: str
    remediation: str


@dataclass
class ScanResult:
    findings: List[Finding] = field(default_factory=list)
    scripts_scanned: List[str] = field(default_factory=list)

    @property
    def has_failures(self) -> bool:
        return any(f.severity in ("high", "medium") for f in self.findings)

    def to_dict(self) -> Dict[str, Any]:
        return {"findings": [asdict(f) for f in self.findings], "scripts_scanned": self.scripts_scanned, "has_failures": self.has_failures}  # fmt: skip


def _check(
    text: str, names: List[str], fp: str, line: int, kind: str
) -> Optional[Finding]:
    """Emit Finding if agent-content params appear in a prompt-like string without delimiters."""
    if not text or _DELIM_RE.search(text):
        return None
    agent = [n for n in names if n in _AGENT_PARAMS]
    if agent and any(kw in text.lower() for kw in _PROMPT_KW):
        return Finding("prompt-injection", "high", fp, line, f"Param(s) {agent} {kind} without delimiters.", "Wrap in <untrusted>...</untrusted>.")  # fmt: skip
    return None


def scan_file(filepath: str) -> List[Finding]:
    """Scan one script for unsafe agent-content interpolation (#1808)."""
    path = _HERE / filepath
    if not path.exists():
        return []
    source = path.read_text(encoding="utf-8")
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError:
        return []
    out: List[Finding] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.JoinedStr):  # f-string
            names = [v.value.id for v in node.values if isinstance(v, ast.FormattedValue) and isinstance(v.value, ast.Name)]  # fmt: skip
            f = _check(ast.get_source_segment(source, node) or "", names, filepath, node.lineno, "interpolated into prompt f-string")  # fmt: skip
            if f:
                out.append(f)
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "format"
        ):
            names = [kw.arg for kw in node.keywords if kw.arg] + [a.id for a in node.args if isinstance(a, ast.Name)]  # fmt: skip
            f = _check(ast.get_source_segment(source, node.func.value) or "", names, filepath, node.lineno, "passed to .format()")  # fmt: skip
            if f:
                out.append(f)
    return out


def run_scan() -> ScanResult:
    """Scan all prompt-builder scripts."""
    result = ScanResult()
    for s in PROMPT_BUILDER_SCRIPTS:
        result.scripts_scanned.append(s)
        result.findings.extend(scan_file(s))
    return result


def format_report(r: ScanResult) -> str:
    """Human-readable pass/fail report."""
    lines = ["=" * 60, f"Prompt-Injection Scan (#1808): {len(r.findings)} finding(s) in {len(r.scripts_scanned)} scripts", "=" * 60]  # fmt: skip
    for f in r.findings:
        lines.append(f"[{f.severity.upper()}] {f.file}:{f.line} — {f.description} → {f.remediation}")  # fmt: skip
    lines.append("RESULT: FAIL" if r.has_failures else "RESULT: PASS")
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