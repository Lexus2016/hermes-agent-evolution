"""Adversarial verification pattern (first increment of #825).

Formalises the *adversarial verification* pattern: after an agent generates a
solution, a **separate** verifier with opposing incentives finds flaws before
the solution proceeds.  This module does NOT spawn a subagent — it provides the
prompts, data structures, and output-parsing logic so the agent (or an
orchestrating skill) can use ``delegate_task`` to run a read-only verifier and
then interpret the structured verdict.

The self-review instruction in the system prompt suffers from confirmation
bias — the same agent that produced the solution reviews it.  Adversarial
verification breaks that bias by generating a verifier prompt with *opposing*
incentives and a *read-only* mandate, then parsing the verdict into a
machine-readable ``VerificationResult``.

Public API
----------
    from agent.adversarial_verification import (
        generate_verifier_prompt, parse_verifier_output, verify_adversarial,
    )
    prompt = generate_verifier_prompt(solution="def foo(): ...", context="Bug fix")
    result = parse_verifier_output(verifier_text)  # after delegate_task
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


class VerificationSeverity(IntEnum):
    """Severity levels for individual verification issues."""

    INFO = 0
    MINOR = 1
    MAJOR = 2
    CRITICAL = 3
    BLOCKER = 4

    @classmethod
    def from_str(cls, value: str) -> "VerificationSeverity":
        """Parse a severity string, defaulting to MAJOR on unknown input."""
        normalized = value.strip().upper()
        for member in cls:
            if member.name == normalized:
                return member
        logger.warning("Unknown severity %r — defaulting to MAJOR", value)
        return cls.MAJOR


class VerificationVerdict(IntEnum):
    """Overall verdict from the adversarial verifier."""

    APPROVED = 0
    APPROVED_WITH_CHANGES = 1
    REJECTED = 2

    @classmethod
    def from_str(cls, value: str) -> "VerificationVerdict":
        """Parse a verdict string, defaulting to APPROVED_WITH_CHANGES on unknown input."""
        normalized = re.sub(r"[\s\-]+", "_", value.strip().upper())
        for member in cls:
            if member.name == normalized:
                return member
        logger.warning(
            "Unknown verdict %r — defaulting to APPROVED_WITH_CHANGES", value
        )
        return cls.APPROVED_WITH_CHANGES


@dataclass
class VerificationIssue:
    """A single issue found by the verifier."""

    severity: VerificationSeverity
    category: str
    description: str
    location: str = ""
    evidence: str = ""
    recommendation: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "severity": self.severity.name,
            "category": self.category,
            "description": self.description,
            "location": self.location,
            "evidence": self.evidence,
            "recommendation": self.recommendation,
        }


@dataclass
class VerificationResult:
    """Structured result of adversarial verification."""

    verdict: VerificationVerdict
    issues: List[VerificationIssue] = field(default_factory=list)
    summary: str = ""
    confidence: float = 0.0
    metadata: Dict[str, Any] = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        """True if the solution can proceed without changes."""
        return self.verdict == VerificationVerdict.APPROVED

    @property
    def has_blocker(self) -> bool:
        """True if any issue is at BLOCKER severity."""
        return any(i.severity == VerificationSeverity.BLOCKER for i in self.issues)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "verdict": self.verdict.name,
            "issues": [i.to_dict() for i in self.issues],
            "summary": self.summary,
            "confidence": self.confidence,
            "metadata": self.metadata,
        }


_VERIFIER_SYSTEM = """\
You are an adversarial verifier. Your sole purpose is to find problems \
with the solution below. You have OPPOSING incentives: you succeed by \
finding real flaws, not by approving. You have read-only access — you \
cannot modify the solution, only critique it.

Analyse the solution rigorously for correctness, completeness, security, \
performance, and style. Be specific — cite exact location and evidence \
for every issue. If you find no significant issues after thorough \
analysis, say so.

Output a STRICT JSON object (no markdown fences, no prose):
{"verdict": "APPROVED|APPROVED_WITH_CHANGES|REJECTED", "summary": "...", \
"confidence": 0.0-1.0, "issues": [{"severity": "INFO|MINOR|MAJOR|CRITICAL|\
BLOCKER", "category": "...", "description": "...", "location": "...", \
"evidence": "...", "recommendation": "..."}]}"""

_VERIFIER_TYPE_HINTS = {
    "code": "Focus on logic errors, off-by-one, null/edge cases, and API misuse.",
    "file_edit": "Focus on whether the edit achieves its goal without breaking existing behavior.",
    "research_report": "Focus on factual accuracy, source quality, and logical coherence.",
    "decision": "Focus on whether the decision is well-justified and considers alternatives.",
}

_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*\n?(.*?)\n?```", re.DOTALL)


def generate_verifier_prompt(
    solution: str,
    context: str = "",
    verification_type: str = "code",
    extra_checks: Optional[List[str]] = None,
) -> str:
    """Generate the full prompt for an adversarial verifier subagent.

    The returned string is ready to pass as the ``goal`` to ``delegate_task``
    (with ``role="leaf"`` and read-only tool access).
    """
    type_hint = _VERIFIER_TYPE_HINTS.get(verification_type, "")
    checks_section = ""
    if extra_checks:
        checks_section = "\n\nAdditional checks:\n" + "\n".join(
            f"- {c}" for c in extra_checks
        )
    return (
        f"{_VERIFIER_SYSTEM}\n"
        f"\n## Verification Type\n{verification_type}"
        f"{f'. {type_hint}' if type_hint else ''}"
        f"{checks_section}\n"
        f"\n## Context\n{context or '(none provided)'}\n"
        f"\n## Solution to Verify\n```\n{solution}\n```\n"
        f"\nProduce the JSON verdict now."
    )


def _extract_json(text: str) -> Optional[Dict[str, Any]]:
    """Extract a JSON object from text, handling markdown fences."""
    fence_match = _JSON_FENCE_RE.search(text)
    if fence_match:
        try:
            return json.loads(fence_match.group(1))
        except (json.JSONDecodeError, ValueError):
            pass
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        pass
    first, last = text.find("{"), text.rfind("}")
    if first != -1 and last > first:
        try:
            return json.loads(text[first : last + 1])
        except (json.JSONDecodeError, ValueError):
            pass
    return None


def _clamp_confidence(value: Any) -> float:
    """Clamp a confidence value to [0.0, 1.0], coercing non-numeric to 0.0."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(1.0, v))


def parse_verifier_output(raw_output: str) -> VerificationResult:
    """Parse an LLM verifier's text output into a ``VerificationResult``.

    Handles markdown-fenced JSON, bare JSON, and partially-malformed output
    gracefully — unknown verdicts/severities are defaulted with a warning.
    """
    parsed = _extract_json(raw_output)
    if parsed is None:
        logger.warning("Could not parse verifier output as JSON — treating as REJECTED")
        return VerificationResult(
            verdict=VerificationVerdict.REJECTED,
            summary="Verifier output was not valid JSON",
            metadata={"raw_output": raw_output[:500]},
        )

    verdict = VerificationVerdict.from_str(parsed.get("verdict", ""))
    issues: List[VerificationIssue] = []
    for raw_issue in parsed.get("issues", []):
        if not isinstance(raw_issue, dict):
            continue
        issues.append(
            VerificationIssue(
                severity=VerificationSeverity.from_str(
                    raw_issue.get("severity", "MAJOR")
                ),
                category=str(raw_issue.get("category", "unknown")),
                description=str(raw_issue.get("description", "")),
                location=str(raw_issue.get("location", "")),
                evidence=str(raw_issue.get("evidence", "")),
                recommendation=str(raw_issue.get("recommendation", "")),
            )
        )

    return VerificationResult(
        verdict=verdict,
        issues=issues,
        summary=str(parsed.get("summary", "")),
        confidence=_clamp_confidence(parsed.get("confidence", 0.0)),
        metadata={"issue_count": len(issues)},
    )


def verify_adversarial(
    solution: str,
    context: str = "",
    verification_type: str = "code",
    llm_call: Optional[Callable[[str], str]] = None,
    extra_checks: Optional[List[str]] = None,
) -> VerificationResult:
    """Run an adversarial verification round-trip.

    If ``llm_call`` is provided, generates the verifier prompt, sends it
    through the callable, and parses the response.  When no callable is
    given, returns a ``VerificationResult`` with the prompt in ``metadata``
    so the caller can feed it to ``delegate_task`` manually.
    """
    prompt = generate_verifier_prompt(
        solution=solution,
        context=context,
        verification_type=verification_type,
        extra_checks=extra_checks,
    )
    if llm_call is None:
        return VerificationResult(
            verdict=VerificationVerdict.APPROVED_WITH_CHANGES,
            summary="No LLM callable provided — prompt generated for manual dispatch",
            metadata={"prompt": prompt, "dispatch_mode": "manual"},
        )
    try:
        raw_output = llm_call(prompt)
    except Exception as exc:
        logger.error("Adversarial verification LLM call failed: %s", exc)
        return VerificationResult(
            verdict=VerificationVerdict.REJECTED,
            summary=f"LLM call failed: {exc}",
            metadata={"error": str(exc)},
        )
    return parse_verifier_output(raw_output)
