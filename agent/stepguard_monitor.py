# -*- coding: utf-8 -*-
"""StepGuard: Learned step-level guardrails with explicit safety-utility balancing (#3245).

Reframes guardrailing as a step-level decision problem:
- Safety head: predicts risk of harmful side-effects (e.g. destructive deletions, privilege escalation, credential exfiltration).
- Utility head: estimates task-completion cost of blocking the action.
- Balancer: gates high-risk / low-utility-penalty actions while preserving benign and mission-critical operations.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# Heuristic patterns for high-risk safety violations (weak supervision / prior)
_HIGH_RISK_PATTERNS = [
    re.compile(r"rm\s+(-[rfRF]+\s+)?(/|\*|~|/\w+)\s*$"),
    re.compile(r"chmod\s+(-R\s+)?777\s+/"),
    re.compile(r":\(\)\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:"),  # fork bomb
    re.compile(r"mkfs\.\w+\s+/dev/\w+"),
    re.compile(r">/dev/sda"),
    re.compile(r"curl.*\|\s*(ba)?sh"),
]

_CREDENTIAL_EXFIL_PATTERNS = [
    re.compile(r"cat\s+.*(\.env|\.netrc|id_rsa|credentials|id_ed25519)\b.*(curl|wget|nc|socat)\b"),
]


@dataclass
class StepGuardDecision:
    allow: bool
    safety_risk: float
    utility_cost: float
    conflict_score: float
    action: str  # "allow", "block", "warn"
    reason: str


def compute_safety_risk(tool_name: str, tool_args: Dict[str, Any]) -> float:
    """Predict safety risk score in [0.0, 1.0]."""
    t = str(tool_name or "").lower()
    if not isinstance(tool_args, dict):
        return 0.0

    if t in ("terminal", "process", "process_manage"):
        cmd = str(tool_args.get("command") or "").strip()
        for p in _HIGH_RISK_PATTERNS:
            if p.search(cmd):
                return 0.95
        for p in _CREDENTIAL_EXFIL_PATTERNS:
            if p.search(cmd):
                return 0.90
        if any(s in cmd for s in ("--no-preserve-root", "dd if=/dev/zero")):
            return 0.95
        return 0.1
    elif t in ("write_file", "patch"):
        path = str(tool_args.get("path") or "").lower()
        if path.startswith(("/etc/", "/usr/", "/boot/", "/dev/")):
            return 0.85
        return 0.05
    return 0.0


def compute_utility_cost(tool_name: str, tool_args: Dict[str, Any]) -> float:
    """Estimate task completion cost (utility) if step is blocked [0.0, 1.0]."""
    t = str(tool_name or "").lower()
    if not isinstance(tool_args, dict):
        return 0.5

    # Check if this action is inherently destructive/malicious
    if compute_safety_risk(tool_name, tool_args) >= 0.90:
        return 0.05

    # Benign inspection and read-only tools have high utility to task completion
    if t in ("read_file", "search_files", "web_search", "session_search"):
        return 0.9
    # File writes and standard commands in workspace have moderate-to-high utility
    if t in ("write_file", "patch"):
        return 0.8
    if t in ("terminal", "process", "process_manage"):
        cmd = str(tool_args.get("command") or "").strip()
        if cmd.startswith(("git ", "pytest ", "python ", "cargo ", "npm ", "ruff ", "ls ", "cat ", "echo ", "mkdir ", "touch ")):
            return 0.85
        return 0.3
    return 0.5


def evaluate_step_guardrail(
    tool_name: str,
    tool_args: Dict[str, Any],
    *,
    risk_threshold: float = 0.80,
    utility_threshold: float = 0.40,
) -> StepGuardDecision:
    """Evaluate step safety vs utility and determine gating decision."""
    safety_risk = compute_safety_risk(tool_name, tool_args)
    utility_cost = compute_utility_cost(tool_name, tool_args)
    conflict_score = safety_risk * (1.0 - utility_cost)

    if safety_risk >= risk_threshold and utility_cost < utility_threshold:
        return StepGuardDecision(
            allow=False,
            safety_risk=round(safety_risk, 3),
            utility_cost=round(utility_cost, 3),
            conflict_score=round(conflict_score, 3),
            action="block",
            reason=f"High safety risk ({safety_risk:.2f}) with low utility cost ({utility_cost:.2f}) triggered StepGuard block.",
        )
    elif safety_risk >= 0.70:
        return StepGuardDecision(
            allow=True,
            safety_risk=round(safety_risk, 3),
            utility_cost=round(utility_cost, 3),
            conflict_score=round(conflict_score, 3),
            action="warn",
            reason=f"Elevated safety risk ({safety_risk:.2f}) requires execution caution.",
        )

    return StepGuardDecision(
        allow=True,
        safety_risk=round(safety_risk, 3),
        utility_cost=round(utility_cost, 3),
        conflict_score=round(conflict_score, 3),
        action="allow",
        reason="StepGuard safety-utility balance approved.",
    )
