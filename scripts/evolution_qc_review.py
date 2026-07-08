#!/usr/bin/env python3
"""Agentic quality-control review for evolution implementations (#796).

Post-implementation review step: a separate leaf subagent reviews completed
work for security, correctness, test coverage, and requirements adherence.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

QC_CATEGORIES: tuple[str, ...] = (
    "security",
    "correctness",
    "test_coverage",
    "requirements",
)
SEVERITY_LEVELS: tuple[str, ...] = ("critical", "high", "medium", "low", "info")
_PASS_KW = frozenset({"pass", "passed", "approved", "lgtm", "no issues"})
_FAIL_KW = frozenset({"fail", "failed", "rejected", "block"})


def build_qc_review_task(
    summary: str,
    files: List[str],
    *,
    issue_number: Optional[int] = None,
    issue_title: Optional[str] = None,
    toolsets: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Build a leaf-worker task dict for delegate_task (QC review subagent)."""
    summary = summary.strip() if isinstance(summary, str) else ""
    files = [f.strip() for f in files if isinstance(f, str) and f.strip()]
    issue_ctx = (
        (
            f"\n\nORIGINATING ISSUE: #{issue_number}"
            + (f" — {issue_title.strip()}" if issue_title else "")
        )
        if issue_number is not None
        else ""
    )
    file_list = "\n".join(f"  - {f}" for f in files) if files else "  (none specified)"
    checklist = "\n".join(
        f"  {i}. **{cat.replace('_', ' ').title()}** — assess and report."
        for i, cat in enumerate(QC_CATEGORIES, 1)
    )
    goal = (
        "You are a QUALITY CONTROL reviewer. A separate agent completed an implementation. "
        f"Review it INDEPENDENTLY before it is declared done.\n\nIMPLEMENTATION SUMMARY:\n{summary}\n\n"
        f"FILES TO REVIEW:\n{file_list}{issue_ctx}\n\nQC CHECKLIST:\n{checklist}\n\n"
        "OUTPUT FORMAT — start with:\n  VERDICT: PASS  — no blocking issues\n"
        "  VERDICT: FAIL  — blocking issues found\n"
        "Then for each finding: [category] [severity] — description\n"
        "Categories: security, correctness, test_coverage, requirements.  Severities: critical, high, medium, low, info."
    )
    return {
        "goal": goal,
        "context": f"QC review: {summary[:200]}" if summary else "QC review.",
        "role": "leaf",
        "toolsets": list(toolsets) if toolsets else ["file"],
    }


_FINDING_RE = re.compile(
    r"\[(?P<cat>security|correctness|test[_ ]coverage|requirements)\]"
    r"\s*\[(?P<sev>critical|high|medium|low|info)\]\s*[—\-]\s*(?P<desc>.+)",
    re.IGNORECASE,
)


def _detect_verdict(text: str) -> str:
    lower = text.lower()
    if m := re.search(r"verdict:\s*(pass|fail)", lower):
        return m.group(1)
    has_fail = any(kw in lower for kw in _FAIL_KW)
    has_pass = any(kw in lower for kw in _PASS_KW)
    if has_fail and not has_pass:
        return "fail"
    if has_pass and not has_fail:
        return "pass"
    return "unknown"


def parse_qc_report(summary: str) -> Dict[str, Any]:
    """Parse a QC subagent's summary into a structured report dict."""
    if not isinstance(summary, str):
        summary = ""
    verdict = _detect_verdict(summary)
    findings: List[Dict[str, str]] = []
    for m in _FINDING_RE.finditer(summary):
        cat = m.group("cat").strip().lower().replace(" ", "_")
        if cat == "testcoverage":
            cat = "test_coverage"
        sev = m.group("sev").strip().lower()
        desc = m.group("desc").strip()
        if cat in QC_CATEGORIES and sev in SEVERITY_LEVELS and desc:
            findings.append({"category": cat, "severity": sev, "description": desc})
    blocking = [f for f in findings if f["severity"] in ("critical", "high")]
    return {
        "verdict": verdict,
        "pass": verdict == "pass",
        "findings": findings,
        "blocking_count": len(blocking),
        "has_blocking": len(blocking) > 0,
    }
