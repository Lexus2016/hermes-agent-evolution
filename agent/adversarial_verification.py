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


# ── Model-family diversity (#909) ────────────────────────────────────────

# Deterministic, offline keyword classifier — no network call, no models.dev
# lookup. Mirrors the family buckets used by optional-skills/security/godmode/
# scripts/auto_jailbreak.py::_detect_model_family, but returns the
# provider/lab name (anthropic/openai/...) rather than a short nickname, so it
# lines up with the provider names used elsewhere in config (delegation.*,
# moa.presets.*.reference_models).
_MODEL_FAMILY_KEYWORDS: list[tuple[str, str]] = [
    ("claude", "anthropic"),
    ("anthropic", "anthropic"),
    ("gpt-", "openai"),
    ("gpt5", "openai"),
    ("openai", "openai"),
    ("o1-", "openai"),
    ("o3-", "openai"),
    ("gemini", "google"),
    ("google", "google"),
    ("grok", "xai"),
    ("x-ai", "xai"),
    ("llama", "meta"),
    ("meta-llama", "meta"),
    ("deepseek", "deepseek"),
    ("qwen", "qwen"),
    ("mistral", "mistral"),
    ("mixtral", "mistral"),
    ("kimi", "moonshot"),
    ("moonshot", "moonshot"),
    ("glm", "zhipu"),
    ("zhipu", "zhipu"),
    ("minimax", "minimax"),
    # "luminous" (Aleph Alpha) must precede "nous" — "nous" is a substring of
    # "luminous", so ordering here prevents a luminous model being misfiled as
    # the Nous Research family.
    ("luminous", "alephalpha"),
    ("nous", "nous"),
    ("hermes", "nous"),
]


def detect_model_family(model: Optional[str]) -> str:
    """Classify a model id string by training lab / provider family.

    Deterministic keyword matching on the model id (no network lookup) — a
    model is in the same family regardless of which provider/base_url routes
    it (e.g. "anthropic/claude-opus-4.8" via OpenRouter and "claude-opus-4-6"
    via the native Anthropic API are both family "anthropic"). Returns
    "unknown" for unrecognized or empty model ids.
    """
    if not model:
        return "unknown"
    model_lower = model.lower()
    for keyword, family in _MODEL_FAMILY_KEYWORDS:
        if keyword in model_lower:
            return family
    return "unknown"


def resolve_verifier_model(
    generator_model: Optional[str], verifier_config: Optional[dict] = None
) -> dict:
    """Resolve whether adversarial verification will run cross-family (#909).

    Same-model-family generator+verifier pairs share blind spots: research
    shows cross-family verification catches ~45% of harmful approvals vs ~6%
    for same-family, and raw self-consistency/agreement is a weak correctness
    signal (rho 0.20-0.59), worst for frontier models (77% agreement, 48%
    wrong on GPQA). This inspects the ``auxiliary.adversarial_verification``
    config the caller passes to ``call_llm(task="adversarial_verification",
    ...)`` and reports whether the resulting call is expected to land on the
    SAME model family as the solution's generator, so the caller can surface
    a warning.

    This function does not itself pick a model or resolve credentials — that
    stays in ``agent.auxiliary_client.call_llm`` /
    ``hermes_cli.runtime_provider.resolve_runtime_provider``, the existing
    single source of truth for provider/model/base_url resolution. It only
    answers "will this probably be the same family?" from the configured
    values, matching how that resolver treats an empty model as "inherit the
    main runtime" (i.e. the same model as the generator).

    Parameters
    ----------
    generator_model : str, optional
        The model id that produced the solution under verification.
    verifier_config : dict, optional
        The ``auxiliary.adversarial_verification`` config section (only
        ``model``/``provider`` are read; other keys are ignored here).

    Returns
    -------
    dict
        ``model`` (the configured override, "" if none), ``provider`` (the
        configured override, "" if none), ``generator_family``,
        ``verifier_family`` ("unknown" when it can't be determined), and
        ``cross_family`` (True/False, or None when it can't be determined —
        e.g. an explicit multi-model provider override such as "openrouter"
        with no explicit model, whose default model's family is unknown
        without a live provider lookup).
    """
    cfg = verifier_config or {}
    configured_model = str(cfg.get("model") or "").strip()
    configured_provider = str(cfg.get("provider") or "").strip()
    generator_family = detect_model_family(generator_model)

    if configured_model:
        verifier_family = detect_model_family(configured_model)
        if generator_model and configured_model.lower() == str(generator_model).lower():
            # Same exact model string — same blind spots — regardless of
            # whether we recognize the family (catches identical unrecognized
            # models that both classify as "unknown").
            cross_family = False
        elif verifier_family != "unknown" and generator_family != "unknown":
            cross_family = verifier_family != generator_family
        else:
            cross_family = None
        return {
            "model": configured_model,
            "provider": configured_provider,
            "generator_family": generator_family,
            "verifier_family": verifier_family,
            "cross_family": cross_family,
        }

    if configured_provider and configured_provider.lower() != "auto":
        # A provider override with no explicit model: the real call uses that
        # provider's default model. For single-family providers the provider
        # name itself identifies the family (e.g. "anthropic", "openai",
        # "google"); multi-model aggregators (e.g. "openrouter") classify as
        # "unknown" and degrade to cross_family=None rather than a guess.
        verifier_family = detect_model_family(configured_provider)
        cross_family = (
            verifier_family != generator_family
            if verifier_family != "unknown" and generator_family != "unknown"
            else None
        )
        return {
            "model": "",
            "provider": configured_provider,
            "generator_family": generator_family,
            "verifier_family": verifier_family,
            "cross_family": cross_family,
        }

    # No override at all ("auto"/empty): the auxiliary task resolver inherits
    # the main runtime, i.e. the SAME model as the generator. That is
    # same-family whenever a generator model exists at all — even if its family
    # is unrecognized, since the verifier runs on that exact same model.
    return {
        "model": "",
        "provider": "",
        "generator_family": generator_family,
        "verifier_family": generator_family,
        "cross_family": False if generator_model else None,
    }


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