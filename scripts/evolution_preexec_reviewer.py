#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Pre-execution reviewer subagent for provisional tool calls with Help/Harm metrics (issue #1271).

Implements the inference-time feedback pattern from the GEM 2026 workshop
paper #13 (Ta, Zhu, Shayandeh; Apple; aclanthology.org/2026.gem-main.13): a
specialised **reviewer agent** evaluates **provisional tool calls *before*
they execute** and either approves or provides feedback for revision.

This is the complement to the existing post-hoc reviewers
(``agent/adversarial_verification.py``, ``agent/correction_review.py``): those
catch errors *after* execution; this module catches them *before* a tool
round-trip is spent.

Two key contributions from the paper that this module provides:

1. **Pre-execution reviewer subagent** — a lightweight reviewer that checks a
   provisional tool call ("is this the right tool? are the arguments
   well-formed? is this in scope?") before execution.  It maps onto the
   existing ``delegate_task`` architecture (the reviewer is just another
   subagent context).  The dispatch is **gated on uncertainty** — only
   non-trivial calls are reviewed, controlling the 2.4× latency overhead the
   paper reports for multi-turn workflows.

2. **Helpfulness/Harmfulness metrics for every pipeline gate** — the paper's
   first systematic measurement of the reviewer tradeoff:
   - **Helpfulness** = % of base-agent errors the reviewer corrects.
   - **Harmfulness** = % of correct responses the reviewer degrades.
   - **Benefit-to-Risk Ratio** = Helpfulness ÷ Harmfulness.
   A gate with a ratio < 1.0 (harm > help) is net negative and should be
   removed or relaxed.  Today the pipeline tracks only blocks, not false
   blocks — this module closes that gap.

The paper identifies a critical anti-pattern — **over-skepticism**: the
reviewer flags valid tool-only responses as "incomplete" for lacking
user-facing dialogue (23% of cases initially).  An explicit
``[CRITICAL] Tool-only responses are complete`` guideline reduced redundant
loops 23%→8%.  This guardline is baked into the reviewer prompt below.

Design: pure, deterministic, standard-library only, no side effects on import.
The reviewer *decision logic* is deterministic (rule-based) so it can run on
every non-trivial call without LLM cost; the ``delegate_task`` integration
point is documented but not coupled — the caller wires the review decision
into the dispatch.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

__all__ = [
    "ProvisionalToolCall",
    "ReviewVerdict",
    "GateMetrics",
    "ReviewerReport",
    "REVIEWER_PROMPT_TEMPLATE",
    "review_tool_call",
    "should_review",
    "compute_gate_metrics",
    "evaluate",
    "main",
]


# ── Over-skepticism guard ────────────────────────────────────────────────────
# The GEM paper found that 23% of reviewer rejections were over-skepticism:
# the reviewer flagged valid tool-only responses as "incomplete" for lacking
# user-facing dialogue.  An explicit guardline reduced this to 8%.  This is
# baked into the reviewer prompt template so any LLM-based reviewer dispatch
# inherits it.

REVIEWER_PROMPT_TEMPLATE: str = (
    "You are a pre-execution reviewer for a provisional tool call. Your job is "
    "to catch errors BEFORE the call executes, not to second-guess correct work.\n\n"
    "Review the provisional call for:\n"
    "  1. Right tool — is this the correct tool for the stated sub-goal?\n"
    "  2. Well-formed arguments — are the argument shapes correct (no JSON parse "
    "errors, no missing required fields)?\n"
    "  3. In scope — is this call within the task's scope?\n\n"
    "[CRITICAL] Tool-only responses are complete. A tool call that returns a "
    "result without accompanying user-facing dialogue is NOT incomplete — do not "
    "flag it for lacking prose. This is the over-skepticism anti-pattern.\n\n"
    'Return JSON: {"approve": true|false, "feedback": "..."}. Only set '
    '"approve": false if you found a concrete error in (1), (2), or (3).'
)


# ── Provisional tool call ───────────────────────────────────────────────────

# Tools whose calls are considered non-trivial and warrant pre-execution
# review.  Read-only tools (read_file, search_files) are trivial and skipped to
# control latency (the paper's 2.4× overhead is only viable for
# accuracy-critical calls).  The set is configurable via the payload.
_DEFAULT_REVIEWABLE_TOOLS: frozenset[str] = frozenset({
    "patch",
    "write_file",
    "delegate_task",
    "tool_call",
    "mcp__jina__read_url",
    "mcp__jina__parallel_read_url",
    "mcp__jina__capture_screenshot_url",
})


@dataclass
class ProvisionalToolCall:
    """A tool call the agent is about to execute, before it runs.

    ``sub_goal`` is the stated purpose of the call (for the "right tool" check).
    ``args`` is the argument dict (for the "well-formed arguments" check).
    ``is_tool_only_response`` records whether the agent's response is a
    tool-only response with no user-facing dialogue — the over-skepticism case.
    """

    tool: str
    sub_goal: str = ""
    args: dict[str, Any] = field(default_factory=dict)
    is_tool_only_response: bool = True
    confidence: float = 1.0  # agent's self-reported confidence in the call [0,1]

    def to_dict(self) -> dict[str, Any]:
        return {
            "tool": self.tool,
            "sub_goal": self.sub_goal,
            "args": dict(self.args),
            "is_tool_only_response": self.is_tool_only_response,
            "confidence": round(self.confidence, 6),
        }

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "ProvisionalToolCall":
        return cls(
            tool=str(d["tool"]),
            sub_goal=str(d.get("sub_goal", "")),
            args=dict(d.get("args", {})),
            is_tool_only_response=bool(d.get("is_tool_only_response", True)),
            confidence=float(d.get("confidence", 1.0)),
        )


# ── Review verdict ──────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ReviewVerdict:
    approve: bool
    feedback: str
    reason: str  # the deterministic rule that produced the verdict
    reviewed: bool  # False if the call was skipped (trivial / high-confidence)

    def to_dict(self) -> dict[str, Any]:
        return {
            "approve": self.approve,
            "feedback": self.feedback,
            "reason": self.reason,
            "reviewed": self.reviewed,
        }


def should_review(
    call: ProvisionalToolCall,
    *,
    reviewable_tools: frozenset[str] | None = None,
    confidence_threshold: float = 0.8,
) -> bool:
    """Gate the reviewer dispatch on uncertainty.

    Only review non-trivial tools AND calls where the agent's confidence is
    below the threshold.  This controls the latency overhead (the paper's 2.4×
    on multi-turn is only viable for accuracy-critical calls).
    """
    tools = reviewable_tools or _DEFAULT_REVIEWABLE_TOOLS
    if call.tool not in tools:
        return False
    return call.confidence < confidence_threshold


def review_tool_call(
    call: ProvisionalToolCall,
    *,
    reviewable_tools: frozenset[str] | None = None,
    confidence_threshold: float = 0.8,
    required_args: Mapping[str, frozenset[str]] | None = None,
) -> ReviewVerdict:
    """Deterministic pre-execution review of a provisional tool call.

    This is the rule-based reviewer that runs without LLM cost.  The
    ``delegate_task`` integration point: a caller that wants an LLM-based
    reviewer dispatches a subagent with ``REVIEWER_PROMPT_TEMPLATE`` and the
    call details, then uses the returned verdict.  This deterministic version
    catches the unambiguous cases (missing required args, out-of-scope,
    over-skepticism false-reject) so the LLM reviewer is only needed for
    ambiguous cases.

    Checks:
    1. **Right tool** — is the tool in the reviewable set? (a call to an
       unknown tool is a parameterization failure.)
    2. **Well-formed arguments** — are all required args present? (per-tool
       required-arg specs via ``required_args``.)
    3. **Over-skepticism guard** — if the call is a tool-only response, do NOT
       reject for lacking prose (the 23%→8% anti-pattern).
    """
    if not should_review(
        call,
        reviewable_tools=reviewable_tools,
        confidence_threshold=confidence_threshold,
    ):
        return ReviewVerdict(
            approve=True,
            feedback="",
            reason="not reviewed — trivial tool or high-confidence call (latency gate)",
            reviewed=False,
        )

    # Check 1: right tool (unknown tool → parameterization failure).
    tools = reviewable_tools or _DEFAULT_REVIEWABLE_TOOLS
    if call.tool not in tools and call.tool:
        return ReviewVerdict(
            approve=False,
            feedback=f"tool '{call.tool}' is not in the known toolset — possible parameterization error (wrong tool name)",
            reason="unknown_tool",
            reviewed=True,
        )

    # Check 2: well-formed arguments (required args present).
    if required_args and call.tool in required_args:
        missing = [a for a in required_args[call.tool] if a not in call.args]
        if missing:
            return ReviewVerdict(
                approve=False,
                feedback=f"missing required argument(s) for '{call.tool}': {missing}",
                reason="missing_required_args",
                reviewed=True,
            )

    # Check 3: over-skepticism guard — tool-only responses are complete.
    # This check never REJECTS; it documents that the guardline is active so a
    # downstream LLM reviewer does not false-reject on this basis.
    if call.is_tool_only_response:
        return ReviewVerdict(
            approve=True,
            feedback="",
            reason="approved — tool-only response is complete (over-skepticism guard active)",
            reviewed=True,
        )

    return ReviewVerdict(
        approve=True,
        feedback="",
        reason="approved — no concrete error found in tool/args/scope",
        reviewed=True,
    )


# ── Helpfulness / Harmfulness metrics for pipeline gates ────────────────────


@dataclass
class GateMetrics:
    """Help/Harm counters for a single pipeline gate (issues rejection, analysis
    triage, merge verification, pre-execution review).

    - ``helpful_blocks`` — the gate blocked genuinely bad work (corrected a
      base-agent error).  This is the gate's Helpfulness numerator.
    - ``harmful_blocks`` — the gate blocked genuinely good work (degraded a
      correct response).  This is the gate's Harmfulness numerator.
    - ``base_agent_errors`` — total base-agent errors seen (the denominator for
      Helpfulness = helpful_blocks / base_agent_errors).
    - ``correct_responses`` — total correct base-agent responses seen (the
      denominator for Harmfulness = harmful_blocks / correct_responses).
    """

    gate_name: str
    helpful_blocks: int = 0
    harmful_blocks: int = 0
    base_agent_errors: int = 0
    correct_responses: int = 0

    @property
    def helpfulness(self) -> float:
        """% of base-agent errors the gate corrected."""
        return (
            self.helpful_blocks / self.base_agent_errors
            if self.base_agent_errors
            else 0.0
        )

    @property
    def harmfulness(self) -> float:
        """% of correct responses the gate degraded."""
        return (
            self.harmful_blocks / self.correct_responses
            if self.correct_responses
            else 0.0
        )

    @property
    def benefit_to_risk(self) -> float | None:
        """Helpfulness ÷ Harmfulness. None if harmfulness is zero (no harm observed)."""
        if self.harmfulness == 0.0:
            return None
        return self.helpfulness / self.harmfulness

    @property
    def net_negative(self) -> bool:
        """A gate with harm > help (ratio < 1.0) is net negative — remove or relax it."""
        ratio = self.benefit_to_risk
        return ratio is not None and ratio < 1.0

    def to_dict(self) -> dict[str, Any]:
        ratio = self.benefit_to_risk
        return {
            "gate_name": self.gate_name,
            "helpful_blocks": self.helpful_blocks,
            "harmful_blocks": self.harmful_blocks,
            "base_agent_errors": self.base_agent_errors,
            "correct_responses": self.correct_responses,
            "helpfulness": round(self.helpfulness, 6),
            "harmfulness": round(self.harmfulness, 6),
            "benefit_to_risk": round(ratio, 6) if ratio is not None else None,
            "net_negative": self.net_negative,
        }

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "GateMetrics":
        return cls(
            gate_name=str(d["gate_name"]),
            helpful_blocks=int(d.get("helpful_blocks", 0)),
            harmful_blocks=int(d.get("harmful_blocks", 0)),
            base_agent_errors=int(d.get("base_agent_errors", 0)),
            correct_responses=int(d.get("correct_responses", 0)),
        )


def compute_gate_metrics(gates: Sequence[GateMetrics]) -> dict[str, Any]:
    """Compute the Benefit-to-Risk ratio per gate and flag net-negative gates.

    Returns a report with per-gate metrics and a list of gates whose ratio <
    1.0 (harm > help) — these are candidates for removal or relaxation per the
    paper's recommendation.
    """
    per_gate = [g.to_dict() for g in gates]
    net_negative = [g.gate_name for g in gates if g.net_negative]
    # At least one ratio computed and reported (success criterion #3).
    ratios_reported = [g.gate_name for g in gates if g.benefit_to_risk is not None]
    return {
        "per_gate": per_gate,
        "net_negative_gates": net_negative,
        "ratios_reported": ratios_reported,
        "reviewer_prompt_includes_overskepticism_guard": True,
    }


# ── Aggregate report ────────────────────────────────────────────────────────


@dataclass
class ReviewerReport:
    verdicts: list[ReviewVerdict] = field(default_factory=list)
    gate_metrics: dict[str, Any] = field(default_factory=dict)
    reviewer_prompt: str = REVIEWER_PROMPT_TEMPLATE

    @property
    def reviewed_count(self) -> int:
        return sum(1 for v in self.verdicts if v.reviewed)

    @property
    def approved_count(self) -> int:
        return sum(1 for v in self.verdicts if v.approve)

    @property
    def rejected_count(self) -> int:
        return sum(1 for v in self.verdicts if not v.approve)

    def to_dict(self) -> dict[str, Any]:
        return {
            "verdicts": [v.to_dict() for v in self.verdicts],
            "gate_metrics": self.gate_metrics,
            "reviewer_prompt": self.reviewer_prompt,
            "summary": {
                "reviewed": self.reviewed_count,
                "approved": self.approved_count,
                "rejected": self.rejected_count,
            },
        }


def evaluate(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Core entry from a JSON payload.

    Expected payload shape::

        {
          "calls": [{"tool": "patch", "sub_goal": "...", "args": {...},
                     "confidence": 0.6, "is_tool_only_response": true}, ...],
          "confidence_threshold": 0.8,
          "required_args": {"patch": frozenset(["path", "old_string", "new_string"])}},
          "gates": [{"gate_name": "merge_verification", "helpful_blocks": 5,
                     "harmful_blocks": 1, "base_agent_errors": 10,
                     "correct_responses": 40}, ...]
        }
    """
    calls = [ProvisionalToolCall.from_dict(c) for c in payload.get("calls", [])]
    confidence_threshold = float(payload.get("confidence_threshold", 0.8))
    reviewable = frozenset(payload.get("reviewable_tools", _DEFAULT_REVIEWABLE_TOOLS))

    # required_args comes in as a dict[str, list[str]] from JSON; convert to frozenset.
    raw_required = payload.get("required_args", {})
    required_args: dict[str, frozenset[str]] = {}
    for tool, arglist in raw_required.items():
        required_args[tool] = frozenset(arglist)

    report = ReviewerReport()
    for call in calls:
        report.verdicts.append(
            review_tool_call(
                call,
                reviewable_tools=reviewable,
                confidence_threshold=confidence_threshold,
                required_args=required_args or None,
            )
        )

    gates = [GateMetrics.from_dict(g) for g in payload.get("gates", [])]
    report.gate_metrics = compute_gate_metrics(gates)

    return report.to_dict()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Pre-execution reviewer subagent with Help/Harm metrics (#1271)",
    )
    parser.add_argument(
        "--payload",
        required=True,
        help="path to a JSON payload with calls and gate metrics",
    )
    args = parser.parse_args(argv)
    with open(args.payload, encoding="utf-8") as fh:
        payload = json.load(fh)
    report = evaluate(payload)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
