#!/usr/bin/env python3
"""Evidence-Span Quality Gate for evolution research (issue #1945).

Parses the research report, extracts findings, checks each for a verbatim
quote + source URL, and writes ``evidence_spans.json`` sidecar.
Pure deterministic gate — no LLM, no network.

Usage: python scripts/evolution_evidence_span_gate.py [--evolution-dir DIR]
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, List

_MAX_SPAN = 200
_URL_RE = re.compile(r"https?://[^\s\])]+")
_HDR_RE = re.compile(r"^###\s+\[(FEATURE|IMPROVEMENT|FIX|BUG)\]\s+(.+)$", re.MULTILINE)
_QUOTE_RE = re.compile(
    r'(?:^>\s+(.+)$)|(?:"([^"]{10,200})")|(?:\u201c([^\u201d]{10,200})\u201d)',
    re.MULTILINE,
)


def _evolution_dir() -> Path:
    env = os.environ.get("EVOLUTION_PROFILE_DIR", "").strip()
    if env:
        return Path(env)
    home = os.environ.get("HERMES_HOME", "").strip() or (Path.home() / ".hermes")
    return Path(home) / "evolution"


def parse_findings(md: str) -> List[Dict[str, Any]]:
    """Extract findings from research report markdown."""
    hdrs = list(_HDR_RE.finditer(md))
    out: List[Dict[str, Any]] = []
    for i, h in enumerate(hdrs):
        body = md[
            h.end() : hdrs[i + 1].start() if i + 1 < len(hdrs) else len(md)
        ].strip()
        qm = _QUOTE_RE.search(body)
        qt = next((g for g in qm.groups() if g), None) if qm else None
        out.append({
            "title": h.group(2).strip(),
            "type": h.group(1),
            "body": body,
            "source_urls": _URL_RE.findall(body),
            "has_quote": bool(qt),
            "quote_text": qt,
            "quote_too_long": len(qt) > _MAX_SPAN if qt else False,
        })
    return out


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
        wq += has_q
        wu += has_u
        nl += is_long
        qt = f["quote_text"]
        detail.append({
            "title": f["title"],
            "type": f["type"],
            "has_quote": has_q,
            "has_url": has_u,
            "quote_too_long": is_long,
            "source_urls": f["source_urls"],
            "verifiable": has_q and has_u and not is_long,
            "quote_preview": (qt[:80] + "...") if qt and len(qt) > 80 else qt,
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
        evolution_dir = _evolution_dir()
    research_dir = evolution_dir / "research"
    if not research_dir.exists():
        return {"total": 0, "skipped": "no research directory"}
    reports = sorted(
        [p for p in research_dir.glob("*.md") if "backup" not in p.name.lower()],
        key=lambda p: p.name,
    )
    if not reports:
        return {"total": 0, "skipped": "no research report found"}
    result = evaluate_findings(parse_findings(reports[-1].read_text(encoding="utf-8")))
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
