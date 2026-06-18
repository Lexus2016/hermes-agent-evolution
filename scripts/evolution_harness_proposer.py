#!/usr/bin/env python3
"""Self-Harness harness-proposal generator (#295, child of #248).

``evolution_trace_miner.py`` (the shipped first increment) turns the agent's own
execution traces into structured **weakness records** — anonymized counts +
classes + labels for clusters that recur >= ``min_count`` times. This module is
the next layer of the Self-Harness loop (arXiv 2606.09498): it feeds those
weakness records to an LLM that emits a **harness proposal** — a single
structured object describing a targeted, constrained change to the agent's
*harness* (the system-prompt, retry policy, or tool guards around the model),
NOT to the task at hand.

Each proposal has a ``type`` in:

  * ``system_prompt_delta``  — text to ADD to the system prompt (a constraint /
    reminder / checklist item the recurring failure shows the model needs).
  * ``retry_policy_change``  — a change to how a tool/provider call is retried
    (e.g. cap consecutive retries, add a backoff, mark a class non-retryable).
  * ``tool_guard``           — a precondition / postcondition guard to wrap a
    tool with (validate args, sanity-check the result, refuse on a bad shape).

The proposal is a structured object the ``evolution-issues`` stage ingests
alongside research-generated proposals (same shape contract: a title + body it
can file as a GitHub issue, with evidence). It is NOT free-form text into the
pipeline — the type is constrained and the evidence is carried explicitly.

CRITICAL SAFETY INVARIANT — HUMAN-GATED, NEVER AUTO-APPLIED
-----------------------------------------------------------
This module is a *generator of proposals*, full stop. It NEVER applies a change:
it does not rewrite a prompt, edit a wrapper, or touch any config. There is no
"apply" code path in this file by design. Every emitted proposal carries
``status="proposed"`` and ``requires_human_review=True``; the regression GATE
that decides whether an accepted proposal is safe to land is the SIBLING issue
#296 and lives elsewhere. A human (or the issues stage's triage) reviews and
vetoes. This mirrors the project decision that the evolution loop proposes, and
only a human disposes.

Design mirrors the other ``scripts/evolution_*.py`` helpers: pure, typed,
import-safe functions + a thin CLI, with the LLM call isolated behind an
INJECTABLE seam (a ``Callable``) so every test runs without a network — exactly
the ``runner`` seam idiom from ``evolution_watchdog.py``.

CLI (reads weakness records from the trace miner's sidecar / stdin):

    evolution_harness_proposer.py weaknesses-latest.json
    cat weaknesses-latest.json | evolution_harness_proposer.py

It prints one JSON object ``{"proposals": [...], "count": N, ...}`` to stdout and
exits 0. WITHOUT an LLM seam configured the CLI is deterministic-only: it still
emits a proposal *envelope* per weakness (type + evidence + a templated body)
but with no LLM-authored delta, so the script is useful and testable offline and
NEVER silently makes a network call.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

# A weakness ``kind`` (from the trace miner) maps to exactly one harness
# proposal ``type``. This is the constrained vocabulary the issues stage relies
# on — anything outside it is dropped rather than passed through as free-form.
PROPOSAL_TYPES = ("system_prompt_delta", "retry_policy_change", "tool_guard")

KIND_TO_TYPE: Dict[str, str] = {
    # A tool whose results keep looking like failures wants a wrapper guard
    # (validate preconditions / sanity-check the result shape).
    "tool_failure": "tool_guard",
    # A recurring provider-layer error class is a retry/fallback-policy concern.
    "provider_error": "retry_policy_change",
    # A retry spiral (same tool many times in a row) is also a retry-policy
    # concern: cap consecutive retries / add a non-retryable diagnostic.
    "retry_spiral": "retry_policy_change",
}

# The signature an injected LLM function must satisfy. It receives the weakness
# record and the chosen proposal type and returns the model-authored fields
# (e.g. ``title``, ``delta``, ``rationale``). It is the ONLY place a network
# call may happen, and it is injected so tests pass a stub. The default is
# ``None`` (offline): no seam -> no LLM-authored delta, never a hidden call.
LLMFn = Callable[[Dict[str, Any], str], Dict[str, Any]]


def proposal_type_for(weakness: Dict[str, Any]) -> Optional[str]:
    """Map a weakness record's ``kind`` to its constrained proposal ``type``.

    Returns ``None`` for an unknown / malformed kind so the caller DROPS it
    rather than inventing a free-form proposal type. Pure + deterministic."""
    if not isinstance(weakness, dict):
        return None
    kind = weakness.get("kind")
    if not isinstance(kind, str):
        return None
    return KIND_TO_TYPE.get(kind)


def _evidence_of(weakness: Dict[str, Any]) -> Dict[str, Any]:
    """Carry the trace evidence forward VERBATIM from the weakness record.

    Only the anonymized fields the miner already emits (counts / classes /
    labels / tool names) — never raw trace content, because the miner never had
    any. Subset is whitelisted so nothing unexpected leaks into a filed issue."""
    allowed = (
        "kind", "tool", "signature", "occurrences", "severity", "label",
        "max_consecutive", "sessions",
    )
    return {k: weakness[k] for k in allowed if k in weakness}


def _default_title(weakness: Dict[str, Any], ptype: str) -> str:
    """Deterministic fallback title used when no LLM seam authors one.

    Concrete and de-dup-friendly (names the subject + the proposal type) so the
    issues stage's exact-title idempotency guard behaves predictably."""
    subject = weakness.get("tool") or weakness.get("signature") or "agent harness"
    label = {
        "system_prompt_delta": "system-prompt guard",
        "retry_policy_change": "retry-policy change",
        "tool_guard": "tool guard",
    }[ptype]
    return f"[HARNESS] {label} for `{subject}`"


def _coerce_str(value: Any) -> str:
    """Best-effort string coercion for an LLM-authored scalar field; never
    raises (a malformed model reply must not crash the generator)."""
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    return str(value)


def build_proposal(
    weakness: Dict[str, Any],
    *,
    llm: Optional[LLMFn] = None,
) -> Optional[Dict[str, Any]]:
    """Turn ONE weakness record into ONE structured harness proposal.

    Returns ``None`` if the weakness's kind has no constrained proposal type
    (it is dropped, not passed through as free-form). The proposal is inert
    DATA describing a change — it is never applied here.

    The LLM seam (``llm``), when provided, authors the human-readable fields
    (``title``, ``delta``, ``rationale``). When absent the proposal is a
    deterministic ENVELOPE: correct type + evidence + a templated body, with
    ``llm_authored=False`` so a reviewer can see it needs fleshing out. Either
    way the proposal carries the hard human-gating fields.
    """
    if not isinstance(weakness, dict):
        return None
    ptype = proposal_type_for(weakness)
    if ptype is None:
        return None

    evidence = _evidence_of(weakness)
    proposal: Dict[str, Any] = {
        "type": ptype,
        "source": "self-harness",  # distinguishes from research-generated proposals
        "evidence": evidence,
        # --- HARD human-gating invariant: inert, never auto-applied. ---
        "status": "proposed",
        "requires_human_review": True,
        "auto_apply": False,
    }

    if llm is not None:
        # The injected seam is the ONLY place a network call may occur. Anything
        # it returns is treated as untrusted scalar text and coerced safely; the
        # ``type`` it cannot change (we picked it deterministically above).
        try:
            authored = llm(dict(weakness), ptype)
        except Exception as exc:  # pragma: no cover - exercised via stub in tests
            # A failing LLM must degrade to the deterministic envelope, never
            # crash the whole batch.
            authored = {}
            proposal["llm_error"] = _coerce_str(exc)
        if not isinstance(authored, dict):
            authored = {}
        title = _coerce_str(authored.get("title")) or _default_title(weakness, ptype)
        proposal["title"] = title
        proposal["delta"] = _coerce_str(authored.get("delta"))
        proposal["rationale"] = _coerce_str(authored.get("rationale"))
        proposal["llm_authored"] = bool(authored)
    else:
        proposal["title"] = _default_title(weakness, ptype)
        proposal["delta"] = ""
        proposal["rationale"] = _coerce_str(weakness.get("label"))
        proposal["llm_authored"] = False

    return proposal


def generate_proposals(
    weaknesses: List[Dict[str, Any]],
    *,
    llm: Optional[LLMFn] = None,
) -> List[Dict[str, Any]]:
    """Map a list of weakness records to harness proposals (best-first).

    Malformed records and records whose kind has no constrained proposal type
    are dropped silently. Order follows the input (the miner already sorts
    weaknesses by severity desc), so the worst cluster yields the first
    proposal. Pure aside from the injected ``llm`` seam."""
    proposals: List[Dict[str, Any]] = []
    for w in weaknesses:
        p = build_proposal(w, llm=llm)
        if p is not None:
            proposals.append(p)
    return proposals


def load_weaknesses(payload: Any) -> List[Dict[str, Any]]:
    """Extract the weakness records list from a trace-miner payload.

    Accepts either the miner's full sidecar object
    (``{"weaknesses": [...], ...}``) or a bare JSON list of records. Non-dict
    entries are dropped. Never raises on a shape it does not recognize — returns
    an empty list so the generator degrades to 'no proposals'."""
    if isinstance(payload, dict):
        records = payload.get("weaknesses", [])
    else:
        records = payload
    if not isinstance(records, list):
        return []
    return [r for r in records if isinstance(r, dict)]


def _parse_args(argv: List[str]) -> Tuple[Optional[str], Optional[str]]:
    """Tiny hand-rolled arg parse (matches the other evolution_* CLIs).

    Returns ``(path, error)``. ``path`` is the positional weakness-records file
    (absent -> read stdin). There are intentionally no flags that could trigger
    an LLM call from the CLI: the offline/deterministic path is the only CLI
    behavior, so the script never makes a hidden network call."""
    path: Optional[str] = None
    for arg in argv[1:]:
        if arg.startswith("-"):
            return None, f"unknown flag: {arg}"
        path = arg
    return path, None


def main(argv: List[str]) -> int:
    path, err = _parse_args(argv)
    if err:
        print(f"[evolution-harness-proposer] {err}", file=sys.stderr)
        return 2
    try:
        raw = Path(path).read_text(encoding="utf-8") if path else sys.stdin.read()
    except OSError as exc:
        print(f"[evolution-harness-proposer] cannot read input: {exc}", file=sys.stderr)
        return 2
    try:
        payload = json.loads(raw)
    except ValueError as exc:
        print(f"[evolution-harness-proposer] input is not valid JSON: {exc}", file=sys.stderr)
        return 2

    weaknesses = load_weaknesses(payload)
    # CLI is deterministic-only: no LLM seam is wired here, so no network call is
    # ever made from the command line. A caller that wants LLM-authored deltas
    # imports ``generate_proposals`` and injects its own ``llm``.
    proposals = generate_proposals(weaknesses, llm=None)

    out = {
        "source": "self-harness",
        "count": len(proposals),
        "human_gated": True,  # echo the invariant for any downstream consumer
        "proposals": proposals,
    }
    print(json.dumps(out, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
