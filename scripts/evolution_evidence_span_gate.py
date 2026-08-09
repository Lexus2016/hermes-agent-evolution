#!/usr/bin/env python3
"""Evidence-span quality gate for evolution research pipeline.

Parses the latest research report, checks each finding for a verbatim
quote and source URL, and writes a JSON sidecar with compliance flags.
Pure deterministic — no LLM, no network.
"""

import json
import re
import sys
from datetime import date
from pathlib import Path

HERMES_HOME = Path(sys.argv[1]) if len(sys.argv) > 1 else Path.home() / ".hermes"
RESEARCH_DIR = HERMES_HOME / "evolution" / "research"
SIDECAR_NAME = "evidence_spans.json"
QUOTE_RE = re.compile(r'"([^"]{5,200})"')
URL_RE = re.compile(r"https?://[^\s\)\],]+")


def find_latest_report(research_dir: Path | None = None) -> Path | None:
    research_dir = research_dir or RESEARCH_DIR
    if not research_dir.is_dir():
        return None
    reports = sorted(research_dir.glob("[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9].md"))
    return reports[-1] if reports else None


def extract_findings(markdown: str) -> list[dict]:
    """Extract findings from '## Key Findings' section's ### headers."""
    lines = markdown.splitlines()
    findings: list[dict] = []
    in_findings = False
    current_header: str | None = None
    current_body: list[str] = []

    for line in lines:
        s = line.strip()
        if s.startswith("## ") and "finding" in s.lower():
            in_findings = True
            continue
        if s.startswith("## ") and in_findings:
            if current_header:
                findings.append(_build(current_header, current_body))
            in_findings = False
            current_header = None
            current_body = []
            continue
        if s.startswith("### ") and in_findings:
            if current_header:
                findings.append(_build(current_header, current_body))
            current_header = s[4:].strip()
            current_body = []
            continue
        if in_findings and current_header:
            current_body.append(line)
    if in_findings and current_header:
        findings.append(_build(current_header, current_body))
    return findings


def _build(header: str, body_lines: list[str]) -> dict:
    body = "\n".join(body_lines)
    quotes = [m.group(1) for m in QUOTE_RE.finditer(body)]
    urls = [m.group(0) for m in URL_RE.finditer(body)]
    has_q, has_u = bool(quotes), bool(urls)
    return {
        "header": header,
        "has_quote": has_q,
        "has_url": has_u,
        "compliant": has_q and has_u,
        "quote": quotes[0] if quotes else None,
        "source_url": urls[0] if urls else None,
    }


def evaluate(findings: list[dict]) -> dict:
    total = len(findings)
    comp = sum(1 for f in findings if f["compliant"])
    return {
        "date": str(date.today()),
        "total_findings": total,
        "compliant": comp,
        "compliance_rate": round(comp / total, 3) if total else 0.0,
        "findings": findings,
    }


def write_sidecar(result: dict, research_dir: Path | None = None) -> Path:
    research_dir = research_dir or RESEARCH_DIR
    research_dir.mkdir(parents=True, exist_ok=True)
    out = research_dir / SIDECAR_NAME
    out.write_text(json.dumps(result, indent=2))
    return out


def run(report_path: Path | None = None) -> dict:
    report = report_path or find_latest_report()
    if not report:
        return {"skipped": "no research report found", "compliance_rate": 0.0}
    findings = extract_findings(report.read_text())
    if not findings:
        return {"skipped": "no findings extracted", "compliance_rate": 0.0}
    result = evaluate(findings)
    write_sidecar(result)
    return result
