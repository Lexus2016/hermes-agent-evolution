#!/usr/bin/env python3
"""Agentic QC review loop (#796): build a QC review task for delegate_task and
parse the subagent's report to gate completion. CLI call sites — no dead code."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

_FAIL = {
    "verdict": "fail",
    "has_blocking": True,
    "issues": [],
    "recommendations": [],
    "ok": False,
}


def _s(v: Any) -> str:
    return v.strip() if isinstance(v, str) else ""


def build_qc_review_task(
    summary: str,
    files: List[str],
    *,
    issue_number: int = 0,
    toolsets: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Build a leaf-subagent QC review task for delegate_task."""
    file_list = "\n".join(f"  - {f}" for f in files) if files else "  (no files listed)"
    goal = (
        "Review the following completed implementation for security, correctness, "
        f"test coverage, and adherence to requirements.\n\n## Summary\n{summary}\n\n"
        f"## Files Changed\n{file_list}\n\n"
        "## Output Format\nReturn JSON:\n"
        '- "verdict": "pass"|"fail", "has_blocking": bool, "issues": [...], "recommendations": [...]\n'
    )
    context = (
        f"You are a quality-control reviewer. Issue #{issue_number}. "
        "Only mark fail+has_blocking for issues that MUST be fixed before merge."
    )
    return {"goal": goal, "context": context, "toolsets": list(toolsets) if toolsets is not None else ["file", "search"], "role": "leaf"}  # fmt: skip


def _extract_json(text: str) -> Optional[dict]:
    """Try direct parse, then ```json fenced block, then bare { ... } block."""
    candidates = [text]
    m1 = re.search(r"```(?:json)?\s*\n(.*?)\n```", text, re.DOTALL)
    m2 = re.search(r"\{.*\}", text, re.DOTALL)
    if m1:
        candidates.append(m1.group(1))
    if m2:
        candidates.append(m2.group(0))
    for c in candidates:
        try:
            raw = json.loads(c)
            if isinstance(raw, dict):
                return raw
        except (ValueError, TypeError):
            pass
    return None


def parse_qc_report(subagent_output: str) -> Dict[str, Any]:
    """Parse QC subagent output. ok=True only when verdict=='pass' and not has_blocking."""
    raw = _extract_json(_s(subagent_output))
    if raw is None:
        return dict(_FAIL)
    verdict = _s(raw.get("verdict")).lower()
    hb = bool(raw.get("has_blocking", False))
    issues = raw.get("issues") if isinstance(raw.get("issues"), list) else []
    recs = raw.get("recommendations") if isinstance(raw.get("recommendations"), list) else []  # fmt: skip
    return {"verdict": verdict, "has_blocking": hb, "issues": issues, "recommendations": recs, "ok": verdict == "pass" and not hb}  # fmt: skip


def _load_text(path: Optional[str]) -> Tuple[str, Optional[str]]:
    try:
        raw = Path(path).read_text(encoding="utf-8") if path else sys.stdin.read()
        return raw, None
    except OSError as exc:
        return "", str(exc)


def _flag(args: List[str], name: str) -> Optional[str]:
    if name in args:
        i = args.index(name)
        if i + 1 < len(args):
            return args[i + 1]
    return None


def main(argv: List[str]) -> int:
    if len(argv) < 2 or argv[1] in ("-h", "--help"):
        print("usage: evolution_qc_review.py {build,parse} ...", file=sys.stderr)
        return 2
    cmd, args = argv[1], argv[2:]
    if cmd == "build":
        summary = _flag(args, "--summary") or ""
        if not _s(summary):
            return 2
        files_str = _flag(args, "--files") or ""
        files = [f.strip() for f in files_str.split(",") if f.strip()]
        try:
            issue_num = int(_flag(args, "--issue") or "0")
        except ValueError:
            issue_num = 0
        task = build_qc_review_task(summary, files, issue_number=issue_num)
        print(json.dumps(task, ensure_ascii=False))
        return 0
    if cmd == "parse":
        path = args[0] if args and not args[0].startswith("-") else None
        data, err = _load_text(path)
        if err:
            return 2
        report = parse_qc_report(data)
        print(json.dumps(report, ensure_ascii=False))
        return 0 if report["ok"] else 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
