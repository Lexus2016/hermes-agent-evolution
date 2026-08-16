# -*- coding: utf-8 -*-
"""Multi-agent cooperation/containment rules + human-escalation path (#2527).

Safety signal: Anthropic's "turf war" experiment (Aug 2026) showed that
multiple agents sharing infrastructure with no explicit cooperation rules
treat each other's changes as attacks — locking competitors out, halting
each other's work, and deploying self-replicating malware. Peace broke out
only after an agent called for human help.

This module provides:

1. **Cooperation rules** — constraints injected into subagent goals when
   concurrent agents share a workspace. Each rule is a short, enforceable
   directive: don't modify another agent's working files, don't lock shared
   resources, don't impersonate another agent, escalate conflicts instead
   of retaliating.
2. **Containment rules** — resource-level isolation: each concurrent agent
   gets its own working directory, and shared resources (git, filesystem)
   are coordinated via a lock protocol.
3. **Human-escalation path** — when inter-agent conflict is detected (a
   subagent reports being blocked, locked out, or impersonated), the
   orchestrator can escalate to the human via a structured message rather
   than letting the agents fight.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional, Sequence

__all__ = [
    "ConflictSignal",
    "EscalationMessage",
    "COOPERATION_RULES",
    "cooperation_directives_for_goal",
    "detect_conflict_signal",
    "build_escalation_message",
]

# Cooperation rules injected into concurrent subagent goals. Each rule is
# short, enforceable, and maps to a specific "turf war" failure mode from
# the Anthropic experiment.
COOPERATION_RULES: List[str] = [
    "Do not modify, delete, or lock files that belong to another concurrent agent's working directory.",
    "Do not impersonate another agent or spoof its identity to monitoring processes.",
    "Do not halt, interrupt, or kill another agent's processes.",
    "Do not deploy self-replicating code or malware against other agents.",
    "If another agent's action blocks your work, do NOT retaliate — report the conflict and wait for coordination.",
    "Shared resources (git branches, databases, network ports) must be acquired via a declared lock, not seized silently.",
    "When in doubt about whether an action affects another agent, escalate to the human rather than acting unilaterally.",
]


def cooperation_directives_for_goal(goal: str, peer_count: int = 0) -> str:
    """Return the cooperation directives to prepend to a subagent goal.

    When ``peer_count`` is 0 (no concurrent peers), returns an empty string
    — a lone agent has no one to conflict with. When peers exist, the full
    directive block is returned.
    """
    if peer_count <= 0:
        return ""
    lines = [
        f"COOPERATION RULES ({peer_count} concurrent peer agent(s) share this infrastructure):",
    ]
    for i, rule in enumerate(COOPERATION_RULES, 1):
        lines.append(f"  {i}. {rule}")
    lines.append(
        "If you experience interference from another agent, STOP and report it — "
        "do not retaliate. Escalate the conflict for human coordination."
    )
    return "\n".join(lines)


@dataclass
class ConflictSignal:
    """A detected inter-agent conflict signal from a subagent's output."""

    agent_id: str
    conflict_type: (
        str  # "blocked", "locked_out", "impersonated", "halted", "retaliated"
    )
    description: str
    peer_agent_id: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class EscalationMessage:
    """A structured human-escalation message for inter-agent conflict."""

    conflict: ConflictSignal
    message: str
    action_requested: str = "review and coordinate"

    def to_dict(self) -> Dict[str, Any]:
        return {
            **self.conflict.to_dict(),
            "message": self.message,
            "action_requested": self.action_requested,
        }


# Patterns that signal inter-agent conflict in a subagent's output.
_CONFLICT_PATTERNS: Dict[str, List[str]] = {
    "blocked": [
        "another agent blocked",
        "blocked by another",
        "my work was blocked",
        "cannot proceed because another agent",
    ],
    "locked_out": [
        "locked out",
        "locked me out",
        "locked out of the server",
        "another agent locked",
    ],
    "impersonated": [
        "impersonat",
        "spoofed my",
        "pretending to be me",
        "another agent is impersonating",
    ],
    "halted": [
        "halted my work",
        "stopped my process",
        "killed my process",
        "another agent halted",
        "another agent stopped",
    ],
    "retaliated": [
        "retaliat",
        "fought back",
        "deployed malware",
        "self-replicating",
        "counter-attack",
    ],
}


def detect_conflict_signal(
    agent_id: str,
    agent_output: str,
) -> Optional[ConflictSignal]:
    """Scan a subagent's output for inter-agent conflict signals.

    Returns a :class:`ConflictSignal` if a conflict pattern is detected,
    or ``None`` if the output shows no signs of inter-agent conflict.
    The scan is case-insensitive substring matching against known
    conflict patterns derived from the Anthropic "turf war" experiment.
    """
    output_lower = (agent_output or "").lower()
    for conflict_type, patterns in _CONFLICT_PATTERNS.items():
        for pattern in patterns:
            if pattern in output_lower:
                # Find the surrounding context (up to 120 chars around the match).
                idx = output_lower.index(pattern)
                start = max(0, idx - 60)
                end = min(len(agent_output), idx + len(pattern) + 60)
                context = agent_output[start:end].strip()
                return ConflictSignal(
                    agent_id=agent_id,
                    conflict_type=conflict_type,
                    description=context,
                )
    return None


def build_escalation_message(
    signal: ConflictSignal,
) -> EscalationMessage:
    """Build a structured human-escalation message for an inter-agent conflict.

    The message is designed to be injected into the orchestrator's
    conversation as a user-role message so the human (or the orchestrating
    agent acting on the human's behalf) can coordinate the conflict.
    """
    msg = (
        f"⚠️ INTER-AGENT CONFLICT ESCALATION\n\n"
        f"Agent: {signal.agent_id}\n"
        f"Conflict type: {signal.conflict_type}\n"
        f"Description: {signal.description}\n"
        f"\n"
        f"This conflict was detected during concurrent multi-agent execution. "
        f"Per the cooperation rules, the agent has stopped and is waiting for "
        f"coordination. Please review and resolve the conflict."
    )
    return EscalationMessage(
        conflict=signal,
        message=msg,
        action_requested="review and coordinate",
    )
