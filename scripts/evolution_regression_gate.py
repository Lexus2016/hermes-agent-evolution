#!/usr/bin/env python3
"""Self-Harness regression gate (#296, child of #248).

The Self-Harness loop (arXiv 2606.09498) has three layers shipped before this
one: ``evolution_trace_miner.py`` mines the agent's own traces into anonymized
**weakness records** (clusters of recurring failures), and
``evolution_harness_proposer.py`` turns the worst clusters into structured,
human-gated **harness proposals**. A human reviews a proposal; if it is accepted
and the harness change ships, the open question this module closes is:

    Did the change actually *help* — or did the targeted weakness get WORSE?

This is the regression GATE. Given (a) the shipped proposal's targeted cluster
signature, (b) the BASELINE occurrence count for that cluster at ship time, and
(c) the trace miner's weakness records over the next N sessions, it decides
whether the cluster *grew*. If it grew, the harness change is a suspected
**regression**, and the gate emits a structured ``regression`` issue object the
``evolution-issues`` stage (and a human) can act on.

CRITICAL SAFETY INVARIANT — HUMAN-VISIBLE, NEVER AUTO-REVERTED
--------------------------------------------------------------
This module *flags*; it never *reverts*. It does not touch the system prompt, a
tool wrapper, git, or any config. There is no "revert" / "rollback" / "apply"
code path in this file by design. The only output is an inert DATA object — a
``regression`` issue carrying ``status="proposed"``, ``requires_human_review=True``
and ``auto_revert=False`` — that goes to the issues stage for a human to triage.
This mirrors the sibling proposer's "the loop proposes, only a human disposes"
contract: the proposer never auto-applies a harness change, and this gate never
auto-reverts one.

Design mirrors ``evolution_harness_proposer.py`` exactly: pure, typed,
import-safe functions + a thin deterministic CLI, with the (optional) LLM that
authors human-readable issue prose isolated behind an INJECTABLE seam (a
``Callable``) so every test runs without a network. The core verdict — did the
cluster grow? — is fully deterministic and needs no LLM at all.

CLI (reads the shipped proposal + post-ship weakness records from one JSON file
or stdin):

    evolution_regression_gate.py gate-input.json
    cat gate-input.json | evolution_regression_gate.py

where the input is ``{"proposal": <shipped harness proposal>, "weaknesses":
[<post-ship weakness records>], "sessions": N}``. It prints one JSON object
``{"regressed": bool, "verdict": {...}, "issue": {...}|null, ...}`` to stdout and
exits 0. WITHOUT an LLM seam the CLI is deterministic-only: it still emits a
fully-formed ``regression`` issue (templated body) when the cluster grew, but
with no LLM-authored prose — so the script is useful and testable offline and
NEVER silently makes a network call.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

# The signature an injected LLM function must satisfy. It receives the regression
# verdict and returns model-authored human-readable fields (e.g. ``title``,
# ``summary``). It is the ONLY place a network call may happen, and it is
# injected so tests pass a stub. The default is ``None`` (offline): no seam ->
# no LLM-authored prose, never a hidden call. Mirrors ``LLMFn`` in the proposer.
LLMFn = Callable[[Dict[str, Any]], Dict[str, Any]]


def _occurrences_of(weakness: Dict[str, Any]) -> int:
    """The comparable cluster size for a weakness record.

    The trace miner emits ``occurrences`` for tool_failure / provider_error and
    ``max_consecutive`` for a retry_spiral; both are the cluster's severity-bearing
    count. Pick whichever the record carries, defaulting to 0. Never raises on a
    malformed value (a non-int degrades to 0 so a bad record can't fake growth)."""
    for key in ("occurrences", "max_consecutive"):
        val = weakness.get(key)
        if isinstance(val, int):
            return val
    return 0


def cluster_signature(weakness: Dict[str, Any]) -> Optional[str]:
    """Stable identity string for a weakness cluster: ``"<kind>:<subject>"``.

    The subject is the tool name (tool_failure / retry_spiral) or the error-class
    signature (provider_error) — exactly the fields the miner keys clusters on.
    Two records describing the SAME recurring failure produce the SAME signature,
    so a post-ship record can be matched to the proposal's targeted cluster.

    Returns ``None`` for a malformed record (no kind, or no subject) so the caller
    cannot accidentally match unrelated clusters. Pure + deterministic."""
    if not isinstance(weakness, dict):
        return None
    kind = weakness.get("kind")
    if not isinstance(kind, str) or not kind:
        return None
    subject = weakness.get("tool")
    if not isinstance(subject, str) or not subject:
        subject = weakness.get("signature")
    if not isinstance(subject, str) or not subject:
        return None
    return f"{kind}:{subject}"


def target_signature(proposal: Dict[str, Any]) -> Optional[str]:
    """The cluster signature a shipped harness proposal targeted.

    A proposal carries its evidence under ``evidence`` (the proposer copies the
    miner's anonymized fields there verbatim); the signature is derived from that
    same kind+subject shape. Falls back to the proposal's top-level fields if an
    older proposal carried them inline. Returns ``None`` when it can't be
    determined, so the gate refuses to judge a proposal it can't tie to a
    cluster. Pure + deterministic."""
    if not isinstance(proposal, dict):
        return None
    evidence = proposal.get("evidence")
    if isinstance(evidence, dict):
        sig = cluster_signature(evidence)
        if sig is not None:
            return sig
    # Fallback: the proposal itself may carry kind+subject (older/inline shape).
    return cluster_signature(proposal)


def find_post_ship_occurrences(
    signature: str,
    weaknesses: List[Dict[str, Any]],
) -> int:
    """Sum the post-ship occurrences for the cluster matching ``signature``.

    Returns 0 when the cluster no longer appears in the post-ship records — the
    desired outcome: the harness change made the weakness vanish below the miner's
    ``min_count`` threshold, so it isn't reported at all. Summing (rather than
    max) is defensive: the miner emits at most one record per cluster, but if a
    caller passes pre-aggregated shards, growth is still measured honestly. Pure."""
    total = 0
    for w in weaknesses:
        if not isinstance(w, dict):
            continue
        if cluster_signature(w) == signature:
            total += _occurrences_of(w)
    return total


def evaluate_regression(
    proposal: Dict[str, Any],
    baseline_occurrences: int,
    post_ship_weaknesses: List[Dict[str, Any]],
    *,
    sessions: int = 0,
) -> Dict[str, Any]:
    """Decide whether a shipped harness change REGRESSED its targeted cluster.

    The verdict is a pure function of three deterministic inputs:
      * the proposal's targeted cluster signature,
      * ``baseline_occurrences`` — the cluster's count at ship time, and
      * the post-ship weakness records over the next N sessions.

    A regression is declared when the cluster GREW: post-ship count strictly
    exceeds the baseline. Equal or smaller is NOT a regression (the change held
    the line or helped). ``delta`` is post - baseline (negative = improvement).

    When the proposal can't be tied to a cluster, the verdict is ``regressed=False``
    with ``reason="no_target_signature"`` — the gate never guesses. The returned
    dict is plain DATA; it applies/reverts nothing.
    """
    sig = target_signature(proposal)
    if sig is None:
        return {
            "regressed": False,
            "reason": "no_target_signature",
            "signature": None,
            "baseline_occurrences": int(baseline_occurrences),
            "post_ship_occurrences": 0,
            "delta": 0,
            "sessions": int(sessions),
        }

    baseline = int(baseline_occurrences)
    post = find_post_ship_occurrences(sig, post_ship_weaknesses)
    delta = post - baseline
    regressed = delta > 0

    return {
        "regressed": regressed,
        "reason": "cluster_grew" if regressed else "cluster_held_or_shrank",
        "signature": sig,
        "baseline_occurrences": baseline,
        "post_ship_occurrences": post,
        "delta": delta,
        "sessions": int(sessions),
    }


def _coerce_str(value: Any) -> str:
    """Best-effort string coercion for an LLM-authored scalar field; never raises
    (a malformed model reply must not crash the gate). Mirrors the proposer."""
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    return str(value)


def _proposal_ref(proposal: Dict[str, Any]) -> Dict[str, Any]:
    """A small, anonymized back-reference to the shipped proposal for the issue.

    Carries only the fields a reviewer needs to find the original (its type,
    source, and accepted-issue number if known) — never the LLM-authored delta
    text, so the regression issue stays a pointer, not a copy."""
    ref: Dict[str, Any] = {}
    for key in ("type", "source", "issue", "accepted_issue", "title"):
        if key in proposal:
            ref[key] = proposal[key]
    return ref


def _default_title(verdict: Dict[str, Any]) -> str:
    """Deterministic ``[REGRESSION]`` title naming the cluster + growth.

    De-dup-friendly (the issues stage's exact-title idempotency guard keys on it)
    and concrete (signature + delta) so re-running the gate on the same window
    produces the same title."""
    sig = verdict.get("signature") or "harness change"
    return f"[REGRESSION] harness change for `{sig}` made the weakness worse"


def _default_body(verdict: Dict[str, Any], proposal_ref: Dict[str, Any]) -> str:
    """Templated, evidence-carrying issue body used when no LLM seam authors one.

    Plain text, deterministic, and self-explanatory so a human (or the issues
    stage) has the full picture without re-running the gate."""
    sig = verdict.get("signature")
    base = verdict.get("baseline_occurrences")
    post = verdict.get("post_ship_occurrences")
    delta = verdict.get("delta")
    sessions = verdict.get("sessions")
    src_issue = proposal_ref.get("accepted_issue") or proposal_ref.get("issue")
    src_line = (
        f"It targets the accepted harness proposal (issue #{src_issue})."
        if src_issue
        else "It targets a shipped harness proposal (source proposal not numbered)."
    )
    return (
        f"The Self-Harness regression gate flagged a shipped harness change as a "
        f"suspected regression.\n\n"
        f"- Targeted weakness cluster: `{sig}`\n"
        f"- Baseline occurrences at ship time: {base}\n"
        f"- Post-ship occurrences over {sessions} session(s): {post}\n"
        f"- Delta: {delta:+d} (cluster GREW after the change shipped)\n\n"
        f"{src_line}\n\n"
        f"The harness change did not reduce — and appears to have worsened — the "
        f"weakness it was meant to fix. A human should review the shipped change "
        f"and decide whether to revert or revise it. This gate only FLAGS; it "
        f"never reverts."
    )


def build_regression_issue(
    verdict: Dict[str, Any],
    proposal: Dict[str, Any],
    *,
    llm: Optional[LLMFn] = None,
) -> Optional[Dict[str, Any]]:
    """Turn a ``regressed=True`` verdict into ONE structured ``regression`` issue.

    Returns ``None`` when the verdict is NOT a regression — no issue is emitted
    for a harness change that held the line or helped. The issue is inert DATA
    the issues stage files; it reverts nothing.

    The LLM seam (``llm``), when provided, authors the human-readable ``title`` and
    ``summary``. When absent the issue is a deterministic ENVELOPE: correct
    kind + evidence + a templated body, with ``llm_authored=False`` so a reviewer
    can see it was machine-templated. Either way the issue carries the hard
    human-gating / no-auto-revert fields.
    """
    if not isinstance(verdict, dict) or not verdict.get("regressed"):
        return None

    proposal_ref = _proposal_ref(proposal if isinstance(proposal, dict) else {})

    issue: Dict[str, Any] = {
        "kind": "regression",
        "source": "self-harness",  # distinguishes from research-generated issues
        "evidence": {
            "signature": verdict.get("signature"),
            "baseline_occurrences": verdict.get("baseline_occurrences"),
            "post_ship_occurrences": verdict.get("post_ship_occurrences"),
            "delta": verdict.get("delta"),
            "sessions": verdict.get("sessions"),
        },
        "targets_proposal": proposal_ref,
        # --- HARD human-gating invariant: inert, never auto-reverted. ---
        "status": "proposed",
        "requires_human_review": True,
        "auto_revert": False,
    }

    if llm is not None:
        # The injected seam is the ONLY place a network call may occur. Anything
        # it returns is treated as untrusted scalar text and coerced safely.
        try:
            authored = llm(dict(verdict))
        except Exception as exc:  # pragma: no cover - exercised via stub in tests
            # A failing LLM must degrade to the deterministic envelope, never
            # crash the gate.
            authored = {}
            issue["llm_error"] = _coerce_str(exc)
        if not isinstance(authored, dict):
            authored = {}
        issue["title"] = _coerce_str(authored.get("title")) or _default_title(verdict)
        issue["body"] = _coerce_str(authored.get("summary")) or _default_body(verdict, proposal_ref)
        issue["llm_authored"] = bool(authored)
    else:
        issue["title"] = _default_title(verdict)
        issue["body"] = _default_body(verdict, proposal_ref)
        issue["llm_authored"] = False

    return issue


def gate(
    proposal: Dict[str, Any],
    baseline_occurrences: int,
    post_ship_weaknesses: List[Dict[str, Any]],
    *,
    sessions: int = 0,
    llm: Optional[LLMFn] = None,
) -> Dict[str, Any]:
    """End-to-end gate: verdict + (conditional) ``regression`` issue.

    Returns ``{"regressed": bool, "verdict": {...}, "issue": {...}|None}``. The
    issue is present ONLY when the cluster grew. Pure aside from the injected
    ``llm`` seam; reverts nothing."""
    verdict = evaluate_regression(
        proposal, baseline_occurrences, post_ship_weaknesses, sessions=sessions
    )
    issue = build_regression_issue(verdict, proposal, llm=llm)
    return {
        "regressed": bool(verdict.get("regressed")),
        "verdict": verdict,
        "issue": issue,
    }


def load_gate_input(payload: Any) -> Tuple[Dict[str, Any], int, List[Dict[str, Any]], int]:
    """Extract ``(proposal, baseline_occurrences, weaknesses, sessions)`` from a
    gate-input payload.

    Accepts the canonical object
    ``{"proposal": {...}, "baseline_occurrences": N, "weaknesses": [...],
       "sessions": M}``. Tolerant of shape: a missing/garbage field degrades to a
    safe default (empty proposal, baseline 0, no weaknesses, 0 sessions) so the
    gate returns 'no regression' rather than raising. The baseline may also be
    read off the proposal's ``evidence.occurrences`` when not given top-level —
    that is exactly the count the miner recorded for the targeted cluster."""
    if not isinstance(payload, dict):
        return {}, 0, [], 0

    proposal = payload.get("proposal")
    if not isinstance(proposal, dict):
        proposal = {}

    baseline = payload.get("baseline_occurrences")
    if not isinstance(baseline, int):
        # Fall back to the count the proposal's own evidence carried at ship time.
        evidence = proposal.get("evidence")
        if isinstance(evidence, dict):
            baseline = _occurrences_of(evidence)
        else:
            baseline = 0

    weaknesses = payload.get("weaknesses")
    if not isinstance(weaknesses, list):
        weaknesses = []
    weaknesses = [w for w in weaknesses if isinstance(w, dict)]

    sessions = payload.get("sessions")
    if not isinstance(sessions, int):
        sessions = 0

    return proposal, baseline, weaknesses, sessions


def _parse_args(argv: List[str]) -> Tuple[Optional[str], Optional[str]]:
    """Tiny hand-rolled arg parse (matches the other evolution_* CLIs).

    Returns ``(path, error)``. ``path`` is the positional gate-input file (absent
    -> read stdin). There are intentionally no flags that could trigger an LLM
    call from the CLI: the offline/deterministic path is the only CLI behavior,
    so the script never makes a hidden network call."""
    path: Optional[str] = None
    for arg in argv[1:]:
        if arg.startswith("-"):
            return None, f"unknown flag: {arg}"
        path = arg
    return path, None


def main(argv: List[str]) -> int:
    path, err = _parse_args(argv)
    if err:
        print(f"[evolution-regression-gate] {err}", file=sys.stderr)
        return 2
    try:
        raw = Path(path).read_text(encoding="utf-8") if path else sys.stdin.read()
    except OSError as exc:
        print(f"[evolution-regression-gate] cannot read input: {exc}", file=sys.stderr)
        return 2
    try:
        payload = json.loads(raw)
    except ValueError as exc:
        print(f"[evolution-regression-gate] input is not valid JSON: {exc}", file=sys.stderr)
        return 2

    proposal, baseline, weaknesses, sessions = load_gate_input(payload)
    # CLI is deterministic-only: no LLM seam is wired here, so no network call is
    # ever made from the command line. A caller that wants LLM-authored prose
    # imports ``gate`` and injects its own ``llm``.
    result = gate(proposal, baseline, weaknesses, sessions=sessions, llm=None)

    out = {
        "source": "self-harness",
        "regressed": result["regressed"],
        "human_gated": True,  # echo the invariant for any downstream consumer
        "auto_revert": False,  # this gate NEVER reverts a harness change
        "verdict": result["verdict"],
        "issue": result["issue"],
    }
    print(json.dumps(out, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
