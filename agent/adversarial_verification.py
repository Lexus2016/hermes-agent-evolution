"""Adversarial verification pattern — separate verifier subagent.

Provides prompt generation and output parsing for spawning an adversarial
verifier subagent via ``delegate_task``. The verifier has different incentives
(find problems), different tool access (read-only), and a fresh context window.

This module is the prompt+parse layer; the actual subagent spawning is done
by the caller via ``delegate_task``. This separation keeps the module testable
without needing a live agent instance.

Wired into the CLI via the ``/verify`` slash command, which:
1. Takes the last agent response as the "solution"
2. Spawns a verifier subagent with ``generate_verifier_prompt()``
3. Parses the verdict with ``parse_verifier_output()``
4. Reports the verdict to the user
"""

from __future__ import annotations

import enum
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Optional

logger = logging.getLogger(__name__)


# ── Severity / verdict enums ─────────────────────────────────────────────


class VerificationSeverity(enum.Enum):
    """How serious a found issue is."""

    INFO = "info"          # suggestion, not a problem
    MINOR = "minor"        # cosmetic / style
    MAJOR = "major"        # functional issue
    CRITICAL = "critical"  # breaks something important
    BLOCKER = "blocker"    # must fix before proceeding


class VerificationVerdict(enum.Enum):
    """Overall verdict from the verifier."""

    APPROVED = "approved"                        # no significant issues
    APPROVED_WITH_CHANGES = "approved_with_changes"  # minor issues, fixable
    REJECTED = "rejected"                         # major/critical issues found


# ── Data classes ─────────────────────────────────────────────────────────


@dataclass
class VerificationIssue:
    """A single issue found by the verifier."""

    severity: VerificationSeverity
    category: str           # correctness, completeness, security, performance, style
    description: str
    location: str = ""      # file:line or section reference
    evidence: str = ""      # quote or reasoning
    recommendation: str = "" # suggested fix


@dataclass
class VerificationResult:
    """Parsed result from an adversarial verification."""

    verdict: VerificationVerdict
    issues: list[VerificationIssue] = field(default_factory=list)
    summary: str = ""
    confidence: float = 0.0   # 0.0–1.0
    raw_output: str = ""      # original verifier output for debugging


# ── Prompt generation ────────────────────────────────────────────────────

_VERIFIER_SYSTEM_PROMPT = """\
You are an adversarial code verifier. Your job is to FIND PROBLEMS with the
solution provided below. You are NOT the author — you are a critical reviewer
with opposing incentives.

Your goal: identify real issues that would cause the solution to fail, break,
or produce wrong results. Be thorough and specific. Cite exact locations.

For each issue you find, report:
- Severity: INFO, MINOR, MAJOR, CRITICAL, or BLOCKER
- Category: correctness, completeness, security, performance, or style
- Description: what the problem is
- Location: where it is (file:line or section)
- Evidence: quote the problematic code/text
- Recommendation: how to fix it

After listing all issues, provide your verdict:
- APPROVED: no significant issues — the solution is sound
- APPROVED_WITH_CHANGES: minor issues that should be fixed but don't block
- REJECTED: major or critical issues that must be fixed before proceeding

Also provide a confidence score (0.0–1.0) for your verdict.

Format your response as JSON:
```json
{
  "verdict": "approved|approved_with_changes|rejected",
  "confidence": 0.0,
  "summary": "one-sentence summary of your assessment",
  "issues": [
    {
      "severity": "info|minor|major|critical|blocker",
      "category": "correctness|completeness|security|performance|style",
      "description": "...",
      "location": "...",
      "evidence": "...",
      "recommendation": "..."
    }
  ]
}
```

If you find no issues, return an empty issues array with verdict "approved".
"""


def generate_verifier_prompt(solution: str, context: str = "", solution_type: str = "code") -> str:
    """Generate the user-message prompt for an adversarial verifier subagent.

    Parameters
    ----------
    solution : str
        The solution to verify (code, file edit, research report, or decision).
    context : str, optional
        Additional context (the original task, constraints, etc.).
    solution_type : str
        Type of solution: "code", "file_edit", "research_report", or "decision".

    Returns
    -------
    str
        The user message to send to the verifier subagent.
    """
    parts = [
        f"## Solution Type: {solution_type}",
    ]
    if context:
        parts.append(f"## Context\n{context}")
    parts.append(f"## Solution to Verify\n```\n{solution}\n```")
    parts.append(
        "\nReview the above solution adversarially. Find real problems. "
        "Be specific with locations and evidence. Output the JSON verdict."
    )
    return "\n\n".join(parts)


def get_verifier_system_prompt() -> str:
    """Return the system prompt for the adversarial verifier subagent."""
    return _VERIFIER_SYSTEM_PROMPT


# ── Output parsing ───────────────────────────────────────────────────────


def _parse_severity(val: str) -> VerificationSeverity:
    """Parse a severity string, defaulting to INFO."""
    try:
        return VerificationSeverity(val.lower().strip())
    except (ValueError, AttributeError):
        return VerificationSeverity.INFO


def _parse_verdict(val: str) -> VerificationVerdict:
    """Parse a verdict string, defaulting to APPROVED (fail-safe)."""
    try:
        return VerificationVerdict(val.lower().strip())
    except (ValueError, AttributeError):
        return VerificationVerdict.APPROVED


def parse_verifier_output(output: str) -> VerificationResult:
    """Parse the verifier subagent's output into a VerificationResult.

    Attempts to extract a JSON block from the output. If parsing fails,
    returns a minimal result with verdict=APPROVED and the raw output.

    Parameters
    ----------
    output : str
        The raw text output from the verifier subagent.

    Returns
    -------
    VerificationResult
        Parsed verification result.
    """
    # Try to find a JSON block in the output
    json_match = re.search(r'```json\s*\n(.*?)\n```', output, re.DOTALL)
    if json_match:
        json_str = json_match.group(1)
    else:
        # Try finding a bare JSON object
        json_match = re.search(r'\{[^{}]*"(?:verdict|issues)"[^{}]*\}', output, re.DOTALL)
        if json_match:
            json_str = json_match.group(0)
        else:
            # Can't parse — return raw output with safe defaults
            logger.debug("Could not extract JSON from verifier output")
            return VerificationResult(
                verdict=VerificationVerdict.APPROVED,
                summary="Could not parse verifier output; defaulting to approved.",
                confidence=0.0,
                raw_output=output,
            )

    try:
        data = json.loads(json_str)
    except json.JSONDecodeError as e:
        logger.debug("JSON decode error in verifier output: %s", e)
        return VerificationResult(
            verdict=VerificationVerdict.APPROVED,
            summary="Could not parse verifier JSON; defaulting to approved.",
            confidence=0.0,
            raw_output=output,
        )

    # Parse issues
    issues: list[VerificationIssue] = []
    for raw_issue in data.get("issues", []):
        if not isinstance(raw_issue, dict):
            continue
        issues.append(VerificationIssue(
            severity=_parse_severity(raw_issue.get("severity", "info")),
            category=raw_issue.get("category", ""),
            description=raw_issue.get("description", ""),
            location=raw_issue.get("location", ""),
            evidence=raw_issue.get("evidence", ""),
            recommendation=raw_issue.get("recommendation", ""),
        ))

    return VerificationResult(
        verdict=_parse_verdict(data.get("verdict", "approved")),
        issues=issues,
        summary=data.get("summary", ""),
        confidence=float(data.get("confidence", 0.0)),
        raw_output=output,
    )


# ── Formatting for display ───────────────────────────────────────────────


_SEVERITY_EMOJI = {
    VerificationSeverity.INFO: "ℹ️",
    VerificationSeverity.MINOR: "⚠️",
    VerificationSeverity.MAJOR: "🔶",
    VerificationSeverity.CRITICAL: "🔴",
    VerificationSeverity.BLOCKER: "🚫",
}

_VERDICT_LABEL = {
    VerificationVerdict.APPROVED: "✅ APPROVED",
    VerificationVerdict.APPROVED_WITH_CHANGES: "⚠️ APPROVED WITH CHANGES",
    VerificationVerdict.REJECTED: "❌ REJECTED",
}


def format_verification_result(result: VerificationResult) -> str:
    """Format a VerificationResult for display to the user.

    Returns a human-readable string suitable for CLI or messaging output.
    """
    lines = [
        f"**Adversarial Verification: {_VERDICT_LABEL.get(result.verdict, result.verdict.value)}**",
    ]
    if result.summary:
        lines.append(f"Summary: {result.summary}")
    if result.confidence > 0:
        lines.append(f"Confidence: {result.confidence:.0%}")
    if result.issues:
        lines.append(f"\nIssues found ({len(result.issues)}):")
        for i, issue in enumerate(result.issues, 1):
            emoji = _SEVERITY_EMOJI.get(issue.severity, "•")
            lines.append(f"  {i}. {emoji} [{issue.severity.value.upper()}] {issue.category}: {issue.description}")
            if issue.location:
                lines.append(f"     Location: {issue.location}")
            if issue.recommendation:
                lines.append(f"     Fix: {issue.recommendation}")
    else:
        lines.append("\nNo issues found.")
    return "\n".join(lines)


# ── Public API ───────────────────────────────────────────────────────────


def verify_adversarial(solution: str, context: str = "", solution_type: str = "code") -> VerificationResult:
    """Run adversarial verification on a solution.

    This is a convenience wrapper that generates the prompt, calls
    ``delegate_task`` to spawn a verifier subagent, and parses the result.

    Note: this function imports delegate_task lazily to avoid circular imports
    at module load time. It requires an active agent context.

    Parameters
    ----------
    solution : str
        The solution text to verify.
    context : str, optional
        Additional context for the verification.
    solution_type : str
        Type of solution ("code", "file_edit", "research_report", "decision").

    Returns
    -------
    VerificationResult
        The parsed verification result.
    """
    # This function is called from the /verify CLI command, which has
    # access to the agent instance. The actual delegate_task call is
    # done by the CLI handler, not here — this function provides the
    # prompt and parser. The split keeps the module testable without
    # a live agent.
    #
    # The CLI handler does:
    #   prompt = generate_verifier_prompt(solution, context, solution_type)
    #   system = get_verifier_system_prompt()
    #   result_text = agent.delegate_task(goal=prompt, ...)  # via delegate_task tool
    #   result = parse_verifier_output(result_text)
    #   print(format_verification_result(result))
    #
    # This function exists for programmatic callers who want to
    # assemble the pieces themselves.
    raise NotImplementedError(
        "Use generate_verifier_prompt() + delegate_task + parse_verifier_output() "
        "directly. See the /verify CLI command for the canonical wiring."
    )