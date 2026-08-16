# -*- coding: utf-8 -*-
"""Subagent deviation monitoring with steer-not-isolate governance (Issue #2487, Slice A, AcMAS).

Monitors fan-out subagent tool-calling behaviors and provenance against learned benign
patterns, emitting actionable steering guidance when behavioral drift is detected.
"""

from __future__ import annotations

import logging
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# High-risk tool categories requiring provenance checks
HIGH_RISK_EXECUTION_TOOLS = frozenset({
    "terminal",
    "execute_code",
    "write_file",
    "patch_file",
    "eval_code",
})
UNTRUSTED_INGESTION_TOOLS = frozenset({
    "web_search",
    "fetch_url",
    "read_untrusted",
    "mcp_web_fetch",
})


@dataclass
class SubagentActionTrace:
    """Record of a single action performed by a subagent with provenance metadata."""

    subagent_id: str
    tool_name: str
    arguments: Dict[str, Any] = field(default_factory=dict)
    provenance_sources: List[str] = field(default_factory=list)
    timestamp_ms: float = field(default_factory=lambda: time.time() * 1000.0)
    risk_score: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class DeviationSignal:
    """Deviation evaluation result indicating trust level and non-isolating steering advice."""

    subagent_id: str
    deviation_score: float
    is_deviating: bool
    steering_action: str  # "continue" | "steer_warning" | "steer_guidance" | "escalate"
    reasons: List[str] = field(default_factory=list)
    suggested_steering_prompt: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class SubagentTrustMonitor:
    """Tracks subagent behaviors and provides steer-not-isolate governance."""

    def __init__(self, deviation_threshold: float = 0.6) -> None:
        self.deviation_threshold = deviation_threshold
        self._history: Dict[str, List[SubagentActionTrace]] = {}
        self._benign_patterns: List[List[str]] = [
            ["read_file", "search", "write_file"],
            ["web_search", "read_file", "calc"],
            ["context_var", "harness", "read_file"],
        ]

    def record_action(
        self,
        subagent_id: str,
        tool_name: str,
        arguments: Dict[str, Any],
        provenance_sources: Optional[List[str]] = None,
    ) -> SubagentActionTrace:
        """Capture subagent tool call, computing baseline risk score and provenance."""
        prov = provenance_sources or []
        risk = 0.0

        if tool_name in HIGH_RISK_EXECUTION_TOOLS:
            risk += 0.4
            # If high-risk execution is fed directly by untrusted web input
            if any("web" in p.lower() or "untrusted" in p.lower() for p in prov):
                risk += 0.4
        elif tool_name in UNTRUSTED_INGESTION_TOOLS:
            risk += 0.2

        trace = SubagentActionTrace(
            subagent_id=subagent_id,
            tool_name=tool_name,
            arguments=arguments,
            provenance_sources=prov,
            risk_score=min(1.0, risk),
        )

        if subagent_id not in self._history:
            self._history[subagent_id] = []
        self._history[subagent_id].append(trace)
        return trace

    def get_action_history(self, subagent_id: str) -> List[SubagentActionTrace]:
        """Return full action history for a specific subagent."""
        return list(self._history.get(subagent_id, []))

    def set_benign_reference(self, patterns: List[List[str]]) -> None:
        """Set expected benign tool-call sequence patterns."""
        self._benign_patterns = patterns

    def evaluate_deviation(
        self,
        subagent_id: str,
        recent_window: int = 5,
    ) -> DeviationSignal:
        """Evaluate deviation from benign reference and produce steering recommendations."""
        history = self._history.get(subagent_id, [])
        if not history:
            return DeviationSignal(
                subagent_id=subagent_id,
                deviation_score=0.0,
                is_deviating=False,
                steering_action="continue",
                reasons=[],
                suggested_steering_prompt=None,
            )

        window_traces = history[-recent_window:]
        recent_tools = [t.tool_name for t in window_traces]
        reasons: List[str] = []
        score = 0.0

        # Check high-risk provenance contamination
        high_risk_traces = [
            t for t in window_traces if t.tool_name in HIGH_RISK_EXECUTION_TOOLS
        ]
        contaminated = [
            t
            for t in high_risk_traces
            if any(
                "web" in p.lower() or "untrusted" in p.lower()
                for p in t.provenance_sources
            )
        ]
        if contaminated:
            score += 0.5
            reasons.append(
                f"Untrusted provenance flow detected into high-risk tool: {[t.tool_name for t in contaminated]}"
            )

        # Check tool sequence pattern deviation
        has_benign_match = False
        for pattern in self._benign_patterns:
            if all(tool in pattern for tool in recent_tools):
                has_benign_match = True
                break

        if not has_benign_match and len(recent_tools) >= 3:
            score += 0.3
            reasons.append(
                f"Tool sequence {recent_tools} deviates from benign references"
            )

        # Repetitive failure / looping check
        if len(recent_tools) >= 3 and len(set(recent_tools)) == 1:
            score += 0.3
            reasons.append(
                f"Subagent in repetitive single-tool loop: {recent_tools[0]}"
            )

        final_score = min(1.0, score)
        is_deviating = final_score >= self.deviation_threshold

        steering_action = "continue"
        steering_prompt = None

        if is_deviating:
            if final_score >= 0.8:
                steering_action = "steer_guidance"
                steering_prompt = (
                    "Caution: Behavioral deviation detected. Please verify your data source provenance, "
                    "avoid executing unvalidated web payloads directly, and summarize your plan before proceeding."
                )
            else:
                steering_action = "steer_warning"
                steering_prompt = "Notice: Unusual tool sequence observed. Please review the goal requirements."

        return DeviationSignal(
            subagent_id=subagent_id,
            deviation_score=final_score,
            is_deviating=is_deviating,
            steering_action=steering_action,
            reasons=reasons,
            suggested_steering_prompt=steering_prompt,
        )


# Global singleton instance for subagent trust monitoring
_GLOBAL_TRUST_MONITOR = SubagentTrustMonitor()


def get_global_trust_monitor() -> SubagentTrustMonitor:
    """Return global SubagentTrustMonitor instance."""
    return _GLOBAL_TRUST_MONITOR
