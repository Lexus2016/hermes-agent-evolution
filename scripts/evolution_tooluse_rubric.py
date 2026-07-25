#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Tool-use competency diagnostic rubric (issue #1268).

Implements the five-dimension diagnostic rubric from MCP-Atlas
(arXiv:2602.00933, Scale AI; "MCP-Atlas: A Large-Scale Benchmark for Tool-Use
Competency with Real MCP Servers") applied to the pipeline's own tool-call
traces (and optionally to agent traces the pipeline builds).

The pipeline tracks cycle-level counts in ``metrics.jsonl`` (merges,
rejections, selected) and has a tool-memory store
(``evolution_tool_memory.py``, #1218) recording per-tool capability and
failure boundaries.  But there is no structured measure of **tool-use
competency** — whether the tool calls are well-chosen and well-formed.  This
module closes that gap with five labeled dimensions scored over existing
tool-call traces:

1. **Discovery** — did the agent find the correct tool, or issue redundant
   ``tool_search``/``tool_describe`` calls before finding it? (Existing
   ``tool_search`` reformulation issue #1144 is this dimension.)
2. **Parameterization** — were argument shapes correct on the first call, or
   did the agent retry with malformed args? (Existing ``tool_call``/
   ``tool_describe`` failure chains #1187/#1242 are this dimension.)
3. **Syntax** — well-formed tool calls (no JSON parse failures).
4. **Error recovery** — after a failed/rejected call, did the agent recover
   (different approach) or loop (identical retry)?  The cheapest
   implementation is a **repeated-identical-call detector** — directly
   addresses the LHTB "infinite debugging loop" finding (2026-07-24) and the
   introspection's tool-retry-spiral pattern (terminal 1884×, read_file 746×,
   search_files 244×, patch 270×, tool_call 271×).
5. **Efficiency** — minimal redundant calls; ratio of unique tool invocations
   to total invocations.

Design: pure, deterministic, standard-library only, no side effects on import.
The rubric is five labeled dimensions applied to existing traces; no new tools
required.  Emit a per-cycle rubric line alongside ``metrics.jsonl`` so tool-use
quality is tracked longitudinally.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

__all__ = [
    "ToolCall",
    "RubricScores",
    "RubricReport",
    "RepeatedCallCluster",
    "score_discovery",
    "score_parameterization",
    "score_syntax",
    "detect_repeated_calls",
    "score_error_recovery",
    "score_efficiency",
    "score_rubric",
    "evaluate",
    "main",
]


# ── Tool-call trace record ──────────────────────────────────────────────────

# Tools used for discovery (finding the right tool without being told its name).
_DISCOVERY_TOOLS: frozenset[str] = frozenset({"tool_search", "tool_describe"})

# Tools that are inherently read-only / safe and never warrant review.
_TRIVIAL_TOOLS: frozenset[str] = frozenset({"read_file", "search_files"})


@dataclass
class ToolCall:
    """A single tool call from a trace, with its arguments, result, and turn index.

    ``succeeded`` records whether the call returned a successful result (vs.
    an error/rejection).  ``error`` is the error message if the call failed.
    ``turn`` is the turn index within the trace (for ordering and
    repeated-call detection).
    """

    tool: str
    args: dict[str, Any] = field(default_factory=dict)
    result: str = ""  # summary of the result
    succeeded: bool = True
    error: str = ""
    turn: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "tool": self.tool,
            "args": dict(self.args),
            "result": self.result,
            "succeeded": self.succeeded,
            "error": self.error,
            "turn": self.turn,
        }

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "ToolCall":
        return cls(
            tool=str(d["tool"]),
            args=dict(d.get("args", {})),
            result=str(d.get("result", "")),
            succeeded=bool(d.get("succeeded", True)),
            error=str(d.get("error", "")),
            turn=int(d.get("turn", 0)),
        )

    @property
    def call_signature(self) -> str:
        """A stable hash of (tool, sorted args) for repeated-call detection."""
        arg_blob = json.dumps(
            self.args, sort_keys=True, ensure_ascii=False, default=str
        )
        return hashlib.sha256(f"{self.tool}|{arg_blob}".encode("utf-8")).hexdigest()[
            :16
        ]


# ── Repeated-identical-call detector (error-recovery dimension) ─────────────


@dataclass(frozen=True)
class RepeatedCallCluster:
    """A run of ≥``threshold`` identical tool+args calls — a recovery failure.

    This directly addresses the LHTB "infinite debugging loop" finding
    (2026-07-24): the agent retries the same failing call identically instead
    of changing approach.  The introspection confirms this pattern at high
    volume (terminal 1884×, read_file 746×, search_files 244×, patch 270×,
    tool_call 271×).
    """

    tool: str
    count: int
    start_turn: int
    end_turn: int
    args_hash: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "tool": self.tool,
            "count": self.count,
            "start_turn": self.start_turn,
            "end_turn": self.end_turn,
            "args_hash": self.args_hash,
        }


def detect_repeated_calls(
    calls: Sequence[ToolCall],
    *,
    threshold: int = 3,
) -> list[RepeatedCallCluster]:
    """Detect runs of ≥``threshold`` identical tool+args calls.

    A "run" is a maximal sequence of consecutive turns where the same
    ``call_signature`` (tool + sorted args) repeats.  Each run of length ≥
    ``threshold`` is a repeated-identical-call cluster — the agent looped
    instead of recovering.
    """
    if not calls:
        return []
    # Sort by turn to ensure temporal order.
    ordered = sorted(calls, key=lambda c: c.turn)
    clusters: list[RepeatedCallCluster] = []
    run_sig: str | None = None
    run_tool: str = ""
    run_start: int = 0
    run_len: int = 0
    for call in ordered:
        if call.call_signature == run_sig:
            run_len += 1
        else:
            # Close the previous run.
            if run_sig is not None and run_len >= threshold:
                clusters.append(
                    RepeatedCallCluster(
                        tool=run_tool,
                        count=run_len,
                        start_turn=run_start,
                        end_turn=ordered[ordered.index(call) - 1].turn
                        if ordered
                        else run_start,
                        args_hash=run_sig,
                    )
                )
            run_sig = call.call_signature
            run_tool = call.tool
            run_start = call.turn
            run_len = 1
    # Close the final run.
    if run_sig is not None and run_len >= threshold:
        clusters.append(
            RepeatedCallCluster(
                tool=run_tool,
                count=run_len,
                start_turn=run_start,
                end_turn=ordered[-1].turn,
                args_hash=run_sig,
            )
        )
    return clusters


# ── Five-dimension scoring ──────────────────────────────────────────────────


def score_discovery(calls: Sequence[ToolCall]) -> float:
    """Discovery dimension: did the agent find the right tool without redundant search?

    Score in [0, 1]: 1.0 = no redundant discovery calls (agent knew the tool or
    found it in one search); lower = more ``tool_search``/``tool_describe``
    calls per non-discovery action.  The penalty is the ratio of discovery
    calls to total non-discovery calls — a high ratio means the agent spent
    most of its calls searching rather than acting.
    """
    if not calls:
        return 1.0
    discovery = sum(1 for c in calls if c.tool in _DISCOVERY_TOOLS)
    acting = sum(1 for c in calls if c.tool not in _DISCOVERY_TOOLS)
    if acting == 0:
        # All calls were discovery — the agent never acted. Worst case.
        return 0.0 if discovery > 0 else 1.0
    ratio = discovery / (discovery + acting)
    # 0 discovery calls → 1.0; ratio approaching 1.0 → 0.0.
    return round(1.0 - ratio, 6)


def score_parameterization(calls: Sequence[ToolCall]) -> float:
    """Parameterization dimension: correct argument shapes on the first call.

    Score in [0, 1]: fraction of calls that succeeded on the first attempt
    with well-formed args (no error, no retry-with-different-args within 3
    turns).  A call that fails and is retried with different args within 3
    turns is a parameterization failure (the first attempt was malformed).
    """
    if not calls:
        return 1.0
    ordered = sorted(calls, key=lambda c: c.turn)
    # Build a map of turn → call for retry detection.
    by_turn = {c.turn: c for c in ordered}
    failed_first: set[int] = set()
    for call in ordered:
        if not call.succeeded:
            # Check if a retry with DIFFERENT args follows within 3 turns.
            for delta in range(1, 4):
                retry = by_turn.get(call.turn + delta)
                if (
                    retry
                    and retry.tool == call.tool
                    and retry.call_signature != call.call_signature
                ):
                    failed_first.add(call.turn)
                    break
    good = sum(1 for c in ordered if c.turn not in failed_first and c.succeeded)
    return round(good / len(ordered), 6)


def score_syntax(calls: Sequence[ToolCall]) -> float:
    """Syntax dimension: well-formed tool calls (no JSON parse failures).

    Score in [0, 1]: fraction of calls with no syntax error.  A syntax error
    is identified by an error message containing parse/json/syntax indicators.
    """
    if not calls:
        return 1.0
    _SYNTAX_ERR = ("json", "parse", "syntax", "malformed", "invalid argument", "decode")
    good = sum(
        1
        for c in calls
        if c.succeeded or not any(e in c.error.lower() for e in _SYNTAX_ERR)
    )
    return round(good / len(calls), 6)


def score_error_recovery(
    calls: Sequence[ToolCall],
    *,
    repeat_threshold: int = 3,
) -> tuple[float, list[RepeatedCallCluster]]:
    """Error-recovery dimension: recover from failures without looping.

    Score in [0, 1]: 1.0 = all failures were followed by a different approach
    (recovery); lower = more repeated-identical-call clusters (looping).
    Returns the score AND the detected repeated-call clusters so the caller
    can report which tools spiralled.
    """
    if not calls:
        return 1.0, []
    clusters = detect_repeated_calls(calls, threshold=repeat_threshold)
    if not clusters:
        return 1.0, []
    # Penalty: each cluster of length L contributes (L - threshold + 1) wasted
    # calls.  Normalise by total calls.
    wasted = sum(c.count - repeat_threshold + 1 for c in clusters)
    penalty = min(1.0, wasted / len(calls))
    return round(1.0 - penalty, 6), clusters


def score_efficiency(calls: Sequence[ToolCall]) -> float:
    """Efficiency dimension: minimal redundant calls.

    Score in [0, 1]: ratio of unique tool invocations (by call_signature) to
    total invocations.  1.0 = every call was unique; lower = more redundancy.
    Note: some redundancy is legitimate (e.g. reading a file after patching
    it), so this is a diagnostic, not a gate.
    """
    if not calls:
        return 1.0
    unique = len({c.call_signature for c in calls})
    return round(unique / len(calls), 6)


# ── Aggregate rubric report ─────────────────────────────────────────────────


@dataclass
class RubricScores:
    discovery: float = 1.0
    parameterization: float = 1.0
    syntax: float = 1.0
    error_recovery: float = 1.0
    efficiency: float = 1.0

    @property
    def overall(self) -> float:
        """Mean of the five dimensions (equal weight, per MCP-Atlas)."""
        return round(
            (
                self.discovery
                + self.parameterization
                + self.syntax
                + self.error_recovery
                + self.efficiency
            )
            / 5.0,
            6,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "discovery": self.discovery,
            "parameterization": self.parameterization,
            "syntax": self.syntax,
            "error_recovery": self.error_recovery,
            "efficiency": self.efficiency,
            "overall": self.overall,
        }


@dataclass
class RubricReport:
    scores: RubricScores = field(default_factory=RubricScores)
    repeated_call_clusters: list[RepeatedCallCluster] = field(default_factory=list)
    total_calls: int = 0
    unique_calls: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "scores": self.scores.to_dict(),
            "repeated_call_clusters": [
                c.to_dict() for c in self.repeated_call_clusters
            ],
            "total_calls": self.total_calls,
            "unique_calls": self.unique_calls,
        }


def score_rubric(
    calls: Sequence[ToolCall],
    *,
    repeat_threshold: int = 3,
) -> RubricReport:
    """Score all five rubric dimensions over a tool-call trace."""
    calls_list = list(calls)
    recovery_score, clusters = score_error_recovery(
        calls_list, repeat_threshold=repeat_threshold
    )
    scores = RubricScores(
        discovery=score_discovery(calls_list),
        parameterization=score_parameterization(calls_list),
        syntax=score_syntax(calls_list),
        error_recovery=recovery_score,
        efficiency=score_efficiency(calls_list),
    )
    return RubricReport(
        scores=scores,
        repeated_call_clusters=clusters,
        total_calls=len(calls_list),
        unique_calls=len({c.call_signature for c in calls_list}),
    )


def evaluate(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Core entry from a JSON payload.

    Expected payload shape::

        {
          "calls": [{"tool": "patch", "args": {...}, "succeeded": true,
                     "error": "", "turn": 0}, ...],
          "repeat_threshold": 3
        }
    """
    calls = [ToolCall.from_dict(c) for c in payload.get("calls", [])]
    threshold = int(payload.get("repeat_threshold", 3))
    report = score_rubric(calls, repeat_threshold=threshold)
    return report.to_dict()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Tool-use competency diagnostic rubric — 5 dimensions (#1268)",
    )
    parser.add_argument(
        "--payload", required=True, help="path to a JSON payload with a tool-call trace"
    )
    args = parser.parse_args(argv)
    with open(args.payload, encoding="utf-8") as fh:
        payload = json.load(fh)
    report = evaluate(payload)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
