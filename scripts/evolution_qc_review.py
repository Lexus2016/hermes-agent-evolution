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
    files: list[str],
    *,
    issue_number: int = 0,
    toolsets: Optional[list[str]] = None,
) -> dict[str, Any]:
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


def parse_qc_report(subagent_output: str) -> dict[str, Any]:
    """Parse QC subagent output. ok=True only when verdict=='pass' and not has_blocking."""
    raw = _extract_json(_s(subagent_output))
    if raw is None:
        return dict(_FAIL)
    verdict = _s(raw.get("verdict")).lower()
    hb = bool(raw.get("has_blocking", False))
    issues = raw.get("issues") if isinstance(raw.get("issues"), list) else []
    recs = raw.get("recommendations") if isinstance(raw.get("recommendations"), list) else []  # fmt: skip
    return {"verdict": verdict, "has_blocking": hb, "issues": issues, "recommendations": recs, "ok": verdict == "pass" and not hb}  # fmt: skip


def _files_from_args(args: list[str]) -> list[str]:
    files_str = _flag(args, "--files") or ""
    return [f.strip() for f in files_str.split(",") if f.strip()]


def build_critic_task(
    summary: str,
    files: list[str],
    *,
    issue_number: int = 0,
    toolsets: Optional[list[str]] = None,
) -> dict[str, Any]:
    """Build a leaf-subagent adversarial critic task for delegate_task (#92).

    Runs AFTER the reviewer has approved a diff: the critic's only job is to
    try to prove the change WRONG with one evidence-grounded, concrete failure
    mode. If it cannot construct such an objection, it must say so explicitly
    — the review then proceeds. This is the structured-disagreement pass that
    breaks reviewer/coder rubber-stamping (Adversarial Review, arXiv:2608.18167).
    """
    file_list = "\n".join(f"  - {f}" for f in files) if files else "  (no files listed)"
    goal = (
        "You are an adversarial code critic. The reviewer has APPROVED the "
        "following implementation. Your job is to try to prove it WRONG.\n\n"
        "Attempt to articulate ONE concrete, evidence-grounded failure mode: a "
        "specific way this change breaks, regresses, or introduces a bug, tied "
        "to the summary or a file below. Do NOT manufacture objections to seem "
        "rigorous — a fabricated disagreement is as harmful as rubber-stamped "
        "approval.\n\n"
        f"## Summary\n{summary}\n\n"
        f"## Files Changed\n{file_list}\n\n"
        "## Output Format\nReturn JSON:\n"
        '- "dissent": bool — true ONLY if you found a concrete failure mode\n'
        '- "grounded": bool — true only if your objection cites specific evidence\n'
        '- "failure_mode": string — the concrete failure mode (empty if none)\n'
        '- "agreement_reason": string — why you could NOT object (empty if dissenting)\n'
    )
    context = (
        f"You are the adversarial critic gate. Issue #{issue_number}. "
        "Only dissent when you can point to a concrete, evidence-grounded "
        "failure mode; otherwise agree explicitly and explain why."
    )
    return {
        "goal": goal,
        "context": context,
        "toolsets": list(toolsets) if toolsets is not None else ["file", "search"],
        "role": "leaf",
    }


def parse_critic_report(subagent_output: str) -> dict[str, Any]:
    """Parse the adversarial critic subagent output (#92).

    A report is ``ok`` when it is coherent: either a grounded dissent (a
    concrete ``failure_mode``) or an explicit agreement with a reason. A
    dissent WITHOUT a failure_mode is un-grounded noise and is not ok.
    """
    raw = _extract_json(_s(subagent_output))
    if raw is None:
        return {
            "dissent": False,
            "grounded": False,
            "failure_mode": "",
            "agreement_reason": "",
            "ok": False,
        }
    dissent = bool(raw.get("dissent", False))
    failure_mode = _s(raw.get("failure_mode"))
    grounded = dissent and bool(failure_mode)
    agreement_reason = _s(raw.get("agreement_reason"))
    ok = grounded if dissent else bool(agreement_reason)
    return {
        "dissent": dissent,
        "grounded": grounded,
        "failure_mode": failure_mode,
        "agreement_reason": agreement_reason,
        "ok": ok,
    }


def adjudicate_review(
    reviewer_report: dict[str, Any],
    critic_report: dict[str, Any],
    *,
    critic_enabled: bool = True,
) -> dict[str, Any]:
    """Deterministic reviewer-to-critic gate (#92).

    Returns ``{"block", "verdict", "reason"}``:
    - reviewer not ok → block ("rework"), regardless of critic.
    - reviewer ok + critic disabled → proceed.
    - reviewer ok + grounded dissent → block ("rework").
    - reviewer ok + no grounded objection → proceed.
    """
    reviewer_ok = bool(reviewer_report.get("ok"))
    if not reviewer_ok:
        return {
            "block": True,
            "verdict": "rework",
            "reason": "reviewer did not approve",
        }
    if not critic_enabled:
        return {
            "block": False,
            "verdict": "proceed",
            "reason": "adversarial critic disabled",
        }
    dissent = bool(critic_report.get("dissent"))
    grounded = bool(critic_report.get("grounded"))
    if dissent and grounded:
        fm = _s(critic_report.get("failure_mode"))
        return {
            "block": True,
            "verdict": "rework",
            "reason": f"critic grounded objection: {fm[:200]}",
        }
    return {
        "block": False,
        "verdict": "proceed",
        "reason": "no grounded objection from critic",
    }


def record_critic_verdict(
    ledger_file: str,
    verdict: dict[str, Any],
    *,
    reviewer_report: dict[str, Any],
    critic_report: dict[str, Any],
) -> None:
    """Record the adversarial-critic verdict into the stage-gate ledger (#92).

    Best-effort instrumentation: a failed import or write must never take the
    review flow down, so any exception is swallowed.
    """
    try:
        from evolution.lib.stage_gate import record_review_critic_verdict

        record_review_critic_verdict(
            Path(ledger_file),
            verdict=str(verdict.get("verdict", "proceed")),
            reviewer_ok=bool(reviewer_report.get("ok")),
            dissent=bool(critic_report.get("dissent")),
            grounded=bool(critic_report.get("grounded")),
            failure_mode=_s(critic_report.get("failure_mode")),
            agreement_reason=_s(critic_report.get("agreement_reason")),
        )
    except Exception:
        pass


def _load_text(path: Optional[str]) -> tuple[str, Optional[str]]:
    try:
        raw = Path(path).read_text(encoding="utf-8-sig") if path else sys.stdin.read()
        return raw, None
    except OSError as exc:
        return "", str(exc)


def _flag(args: list[str], name: str) -> Optional[str]:
    if name in args:
        i = args.index(name)
        if i + 1 < len(args):
            return args[i + 1]
    return None


def main(argv: list[str]) -> int:
    if len(argv) < 2 or argv[1] in ("-h", "--help"):
        print(
            "usage: evolution_qc_review.py "
            "{build,parse,build-critic,parse-critic,adjudicate} ...",
            file=sys.stderr,
        )
        return 2
    cmd, args = argv[1], argv[2:]
    if cmd == "build":
        summary = _flag(args, "--summary") or ""
        if not _s(summary):
            return 2
        files = _files_from_args(args)
        try:
            issue_num = int(_flag(args, "--issue") or "0")
        except ValueError:
            issue_num = 0
        task = build_qc_review_task(summary, files, issue_number=issue_num)
        print(json.dumps(task, ensure_ascii=False))
        return 0
    if cmd == "build-critic":
        summary = _flag(args, "--summary") or ""
        if not _s(summary):
            return 2
        files = _files_from_args(args)
        try:
            issue_num = int(_flag(args, "--issue") or "0")
        except ValueError:
            issue_num = 0
        task = build_critic_task(summary, files, issue_number=issue_num)
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
    if cmd == "parse-critic":
        path = args[0] if args and not args[0].startswith("-") else None
        data, err = _load_text(path)
        if err:
            return 2
        report = parse_critic_report(data)
        print(json.dumps(report, ensure_ascii=False))
        return 0 if report["ok"] else 1
    if cmd == "adjudicate":
        reviewer_path = _flag(args, "--reviewer")
        critic_path = _flag(args, "--critic")
        if not reviewer_path or not critic_path:
            return 2
        rev_data, rev_err = _load_text(reviewer_path)
        crit_data, crit_err = _load_text(critic_path)
        if rev_err or crit_err:
            return 2
        reviewer = parse_qc_report(rev_data)
        critic = parse_critic_report(crit_data)
        verdict = adjudicate_review(
            reviewer, critic, critic_enabled="--no-critic" not in args
        )
        ledger = _flag(args, "--ledger")
        if ledger:
            record_critic_verdict(
                ledger, verdict, reviewer_report=reviewer, critic_report=critic
            )
        print(json.dumps(verdict, ensure_ascii=False))
        return 1 if verdict["block"] else 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
