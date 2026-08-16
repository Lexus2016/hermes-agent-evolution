# -*- coding: utf-8 -*-
"""Gate autonomy behind verification + 'safeguards disabled + internet' red-line (#2528).

Safety signal: Anthropic's Aug 2026 risk report raised its catastrophic-
misalignment rating, citing a UK AISI evaluation in which a frontier model,
with safeguards disabled and internet access enabled, "engaged in sustained,
potentially harmful activity directed at real people and organisations."

This module provides:

1. **Autonomy gate** — a deterministic check that gates autonomous action
   behind verification. A high-risk autonomous action (one that mutates
   external state, sends messages, or touches the network) is only allowed
   when the caller can demonstrate it has been verified (e.g. a test passed,
   a dry-run succeeded, a human approved). Unverified high-risk actions are
   blocked.

2. **Red-line test configuration** — a detector for the "safeguards disabled
   + internet access" configuration. When both conditions are present, the
   configuration is flagged as a red-line test that must not be run against
   real people or organisations.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional, Sequence

__all__ = [
    "AutonomyVerdict",
    "RedLineVerdict",
    "HIGH_RISK_ACTIONS",
    "check_autonomy_gate",
    "check_red_line_config",
]

# High-risk autonomous actions that must be gated behind verification.
# Each entry: (action_name, description).
HIGH_RISK_ACTIONS: List[str] = [
    "send_message",
    "create_tweet",
    "git_push",
    "delete_file",
    "delete_directory",
    "network_request",
    "execute_remote",
    "modify_system_config",
    "install_package",
    "send_email",
]


@dataclass
class AutonomyVerdict:
    """Result of the autonomy gate on a proposed high-risk action."""

    action: str
    allowed: bool
    reason: str
    verification_required: bool = True

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "AutonomyVerdict":
        return cls(
            action=str(d.get("action", "")),
            allowed=bool(d.get("allowed", False)),
            reason=str(d.get("reason", "")),
            verification_required=bool(d.get("verification_required", True)),
        )


@dataclass
class RedLineVerdict:
    """Result of the red-line configuration check."""

    is_red_line: bool
    safeguards_disabled: bool
    internet_enabled: bool
    reason: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "RedLineVerdict":
        return cls(
            is_red_line=bool(d.get("is_red_line", False)),
            safeguards_disabled=bool(d.get("safeguards_disabled", False)),
            internet_enabled=bool(d.get("internet_enabled", False)),
            reason=str(d.get("reason", "")),
        )


def check_autonomy_gate(
    action: str,
    *,
    verified: bool = False,
    verification_evidence: str = "",
    human_approved: bool = False,
) -> AutonomyVerdict:
    """Gate a high-risk autonomous action behind verification.

    A high-risk action (one in :data:`HIGH_RISK_ACTIONS`) is only allowed
    when it has been verified (``verified=True`` with evidence) OR a human
    has approved it (``human_approved=True``). Otherwise it is blocked.

    A non-high-risk action is always allowed (it does not need the gate).
    """
    action_lower = (action or "").lower()
    is_high_risk = any(hr in action_lower for hr in HIGH_RISK_ACTIONS)

    if not is_high_risk:
        return AutonomyVerdict(
            action=action,
            allowed=True,
            reason="Not a high-risk action — no verification required.",
            verification_required=False,
        )

    if human_approved:
        return AutonomyVerdict(
            action=action,
            allowed=True,
            reason="Human approved the high-risk action.",
        )

    if verified and verification_evidence:
        return AutonomyVerdict(
            action=action,
            allowed=True,
            reason=f"Verified: {verification_evidence}",
        )

    if verified and not verification_evidence:
        return AutonomyVerdict(
            action=action,
            allowed=False,
            reason="Marked verified but no verification evidence provided — cannot confirm.",
        )

    return AutonomyVerdict(
        action=action,
        allowed=False,
        reason=(
            f"High-risk action '{action}' is not verified — gate autonomy behind "
            f"verification before executing."
        ),
    )


def check_red_line_config(
    *,
    safeguards_disabled: bool = False,
    internet_enabled: bool = False,
) -> RedLineVerdict:
    """Detect the 'safeguards disabled + internet access' red-line config.

    When BOTH safeguards are disabled AND internet access is enabled, the
    configuration is a red-line test (per the Anthropic risk report) that
    must not be run against real people or organisations.
    """
    if safeguards_disabled and internet_enabled:
        return RedLineVerdict(
            is_red_line=True,
            safeguards_disabled=True,
            internet_enabled=True,
            reason=(
                "RED-LINE CONFIGURATION: safeguards disabled + internet access "
                "enabled. This configuration must not be run against real people "
                "or organisations (Anthropic risk report, Aug 2026)."
            ),
        )
    return RedLineVerdict(
        is_red_line=False,
        safeguards_disabled=safeguards_disabled,
        internet_enabled=internet_enabled,
        reason="Not a red-line configuration.",
    )
