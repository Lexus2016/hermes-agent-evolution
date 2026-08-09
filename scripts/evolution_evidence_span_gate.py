#!/usr/bin/env python3
"""Evidence-Span Quality Gate for evolution research (issue #1945).

Every research finding must carry a short verbatim quote from its source
that directly supports the claim. Findings without verifiable evidence
spans are flagged so the analysis stage can filter or downgrade them.

Pure deterministic gate — no LLM, no network. Parses the research report
markdown, extracts findings, checks each for required fields, and writes
a JSON sidecar (``evidence_spans.json``) for the analysis stage.

Usage: python scripts/evolution_evidence_span_gate.py [--evolution-dir DIR]
"""

from __future__ import annotations
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, List

_MAX_SPAN_LEN = 200
_URL_RE = re.compile(r"https?://[^\s\])]+")
_FINDING_HEADER_RE = re.compile(
    r"^###\s+\[(FEATURE|IMPROVEMENT|FIX|BUG)\]\s+(.+)$", re.MULTILINE
)
_QUOTE_RE = re.compile(
    r'(?:^>\s+(.+)$)|(?:"([^"]{10,200})")|(?:\u201c([^\u201d]{10,200})\u201d)',
    re.MULTILINE,
)


def _default_evolution_dir() -> Path:
    """Resolve the evolution profile directory (profile-aware)."""
    env = os.environ.get("EVOLUTION_PROFILE_DIR", "").strip()
    if env:
        return Path(env)
    hermes_home = Path(
        os.environ.get("HERMES_HOME", "").strip() or (Path.home() / ".hermes")
    )
    return hermes_home / "evolution"


def parse_findings(markdown: str) -> List[Dict[str, Any]]:
    """Extract individual findings from the research report markdown."""
    headers = list(_FINDING_HEADER_RE.finditer(markdown))
    findings: List[Dict[str, Any]] = []
    for i, hdr in enumerate(headers):
        start = hdr.end()
        end = headers[i + 1].start() if i + 1 < len(headers) else len(markdown)
        body = markdown[start:end].strip()
        urls = _URL_RE.findall(body)
        qm = _QUOTE_RE.search(body)
        qt = next((g for g in qm.groups() if g is not None), None) if qm else None
        findings.append({
            "title": hdr.group(2).strip(),
            "type": hdr.group(1),
            "body": body,
            "source_urls": urls,
            "has_quote": qt is not None,
            "quote_text": qt,
            "quote_too_long": len(qt) > _MAX_SPAN_LEN if qt else False,
        })
    return findings


def evaluate_findings(findings: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Evaluate findings against evidence-span requirements."""
    detail: List[Dict[str, Any]] = []
    wq = wu = nl = 0
    for f in findings:
        has_q, has_u, is_long = (
            f["has_quote"],
            bool(f["source_urls"]),
            f["quote_too_long"],
        )
        if has_q:
            wq += 1
        else:
            pass
        if has_u:
            wu += 1
        if is_long:
            nl += 1
        verifiable = has_q and has_u and not is_long
        detail.append({
            "title": f["title"],
            "type": f["type"],
            "has_quote": has_q,
            "has_url": has_u,
            "quote_too_long": is_long,
            "source_urls": f["source_urls"],
            "verifiable": verifiable,
            "quote_preview": (
                f["quote_text"][:80] + "..."
                if f["quote_text"] and len(f["quote_text"]) > 80
                else f["quote_text"]
            ),
        })
    total = len(findings)
    both = sum(1 for d in detail if d["has_quote"] and d["has_url"])
    return {
        "total": total,
        "with_quote": wq,
        "without_quote": total - wq,
        "with_url": wu,
        "without_url": total - wu,
        "too_long": nl,
        "compliance_rate": round(both / total, 3) if total else 0.0,
        "findings": detail,
    }


def run_gate(evolution_dir: Path | None = None) -> Dict[str, Any]:
    """Run the evidence-span gate on the latest research report."""
    if evolution_dir is None:
        evolution_dir = _default_evolution_dir()
    research_dir = evolution_dir / "research"
    if not research_dir.exists():
        return {"total": 0, "skipped": "no research directory"}
    reports = sorted(
        [p for p in research_dir.glob("*.md") if "backup" not in p.name.lower()],
        key=lambda p: p.name,
    )
    if not reports:
        return {"total": 0, "skipped": "no research report found"}
    markdown = reports[-1].read_text(encoding="utf-8")
    result = evaluate_findings(parse_findings(markdown))
    result["report_file"] = reports[-1].name
    out = evolution_dir / "evidence_spans.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, indent=2, sort_keys=True), encoding="utf-8")
    return result


def format_summary(result: Dict[str, Any]) -> str:
    """One-line summary for the pipeline log."""
    if result.get("skipped"):
        return f"[evidence-span-gate] skipped: {result['skipped']}"
    return (
        f"[evidence-span-gate] {result['total']} findings: "
        f"compliance={result.get('compliance_rate', 0.0):.0%} "
        f"(missing_quote={result.get('without_quote', 0)} "
        f"missing_url={result.get('without_url', 0)} "
        f"too_long={result.get('too_long', 0)})"
    )


def main(argv: List[str]) -> int:
    evolution_dir: Path | None = None
    args = argv[1:]
    if "--evolution-dir" in args:
        i = args.index("--evolution-dir")
        if i + 1 < len(args):
            evolution_dir = Path(args[i + 1])
    print(format_summary(run_gate(evolution_dir)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
