# -*- coding: utf-8 -*-
"""Code-diff proposal schema + generator for the retry-policy surface (#2613).

Harness-R1 (parent #2525) — the "apply-path to executable harness code".

``scripts/evolution_harness_proposer.py`` emits config-level PROSE proposals
(``retry_policy_change`` as a structured *text* delta, ``PROPOSAL_TYPES`` tuple
line 65). That is a description of a change, not a change a machine can apply.
This module is the genuinely-new next layer: a **structured code-diff proposal**
for the retry-policy surface — the retry count, backoff schedule, and guard
conditions that live in the harness's executable code (e.g.
``agent/retry_utils.py``, ``agent/turn_retry_state.py``) — plus a generator that
turns a failure trace / retry-policy change request into that diff.

What this module provides:

1. **A JSON schema** (:data:`RETRY_POLICY_DIFF_SCHEMA`) describing a code-diff
   proposal: target file, function/symbol, before/after code blocks, the
   retry-count / backoff / guard-condition changes, and a structured diff delta
   (line hunks with add/remove/context entries).
2. **A generator** (:func:`generate_code_diff`) that, given a
   :class:`RetryPolicyChange` request, synthesizes the ``after`` block by
   applying the requested change to the retry-policy surface of the ``before``
   block, computes the structured diff delta, and emits a
   :class:`CodeDiffProposal`.
3. **A validator** (:func:`validate_proposal`) that checks a proposal dict
   against the schema (stdlib-only — no ``jsonschema`` dependency).

CRITICAL SAFETY INVARIANT — HUMAN-GATED, NEVER AUTO-APPLIED
-----------------------------------------------------------
Like its sibling ``scripts/evolution_harness_proposer.py``, this module is a
*generator of proposals*, full stop. It NEVER applies a change: it does not
rewrite a file, edit a wrapper, or touch any config. There is no "apply" code
path in this file by design. Every emitted proposal carries
``status="proposed"``, ``requires_human_review=True`` and ``auto_apply=False``;
a human (or the issues stage's triage) reviews and vetoes before any diff is
ever applied to the harness.

Pure, deterministic, import-safe — all IO is explicit and none is performed
here. No external dependencies beyond the stdlib.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

__all__ = [
    "RETRY_POLICY_DIFF_SCHEMA",
    "BackoffSpec",
    "RetryPolicyChange",
    "CodeDiffProposal",
    "validate_proposal",
    "generate_code_diff",
    "diff_lines",
]

# ── Retry-policy surface patterns ─────────────────────────────────────────
# The generator edits the retry-policy surface of a harness code block by
# matching these canonical forms. Each pattern captures the *prefix* (the
# assignment / keyword text that must be preserved verbatim) and the *value*
# (the part the change replaces). A requested surface that is not found in the
# ``before`` block is left unchanged and recorded as a warning — the generator
# never guesses or invents code (fail-safe, deterministic).

# Retry count: ``max_retries = 3`` / ``max_retries=3`` (also ``max_attempts``).
_RETRY_COUNT_RE = re.compile(
    r"(?P<prefix>\b(?:max_retries|max_attempts)\s*=\s*)(?P<value>\d+)"
)
# Backoff base delay: ``base_delay = 5.0`` / ``base_delay=5.0``.
_BACKOFF_BASE_RE = re.compile(r"(?P<prefix>\bbase_delay\s*=\s*)(?P<value>[\d.]+)")
# Backoff max delay: ``max_delay = 120.0`` / ``max_delay=120.0``.
_BACKOFF_MAX_RE = re.compile(r"(?P<prefix>\bmax_delay\s*=\s*)(?P<value>[\d.]+)")
# Guard condition: the first ``if <cond>:`` line in the block. The condition
# expression (between ``if`` and ``:``) is what the change replaces.
_GUARD_RE = re.compile(r"(?m)^(?P<indent>\s*)if\s+(?P<cond>[^:]+):")


# ── JSON schema ───────────────────────────────────────────────────────────
# Draft-07-style JSON schema for a code-diff proposal on the retry-policy
# surface. Kept as a plain dict so it can be embedded in a proposal, shipped
# to a reviewer, or validated by any JSON-schema tooling without importing a
# third-party library.
RETRY_POLICY_DIFF_SCHEMA: Dict[str, Any] = {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "$id": "hermes://evolution/code-diff-proposal/retry-policy/v1",
    "title": "RetryPolicyCodeDiffProposal",
    "description": (
        "A structured code-diff proposal against the retry-policy surface of "
        "the harness code (retry count, backoff schedule, guard conditions). "
        "Inert data describing a change — never auto-applied."
    ),
    "type": "object",
    "required": [
        "type",
        "source",
        "target_file",
        "symbol",
        "before",
        "after",
        "retry_policy",
        "diff_delta",
        "status",
        "requires_human_review",
        "auto_apply",
    ],
    "properties": {
        "type": {
            "const": "retry_policy_code_diff",
            "description": "Fixed discriminator for this proposal kind.",
        },
        "source": {
            "const": "self-harness",
            "description": "Distinguishes from research-generated proposals.",
        },
        "target_file": {
            "type": "string",
            "minLength": 1,
            "description": "Repo-relative path of the harness file to edit.",
        },
        "symbol": {
            "type": "string",
            "minLength": 1,
            "description": "Function / symbol name the change targets.",
        },
        "before": {
            "type": "string",
            "description": "Current source block (before the change).",
        },
        "after": {
            "type": "string",
            "description": "Proposed source block (after the change).",
        },
        "retry_policy": {
            "type": "object",
            "description": "The retry-policy surface changes being proposed.",
            "properties": {
                "retry_count": {
                    "type": ["integer", "null"],
                    "minimum": 0,
                    "description": "New max retry count, or null if unchanged.",
                },
                "backoff": {
                    "type": ["object", "null"],
                    "description": "New backoff schedule, or null if unchanged.",
                    "properties": {
                        "base_delay": {"type": "number", "minimum": 0.0},
                        "max_delay": {"type": "number", "minimum": 0.0},
                        "jitter_ratio": {"type": "number", "minimum": 0.0},
                    },
                    "required": ["base_delay"],
                },
                "guard_condition": {
                    "type": ["string", "null"],
                    "description": "New guard condition expression, or null.",
                },
            },
        },
        "diff_delta": {
            "type": "array",
            "description": "Structured diff delta (line hunks).",
            "items": {
                "type": "object",
                "required": [
                    "old_start",
                    "old_count",
                    "new_start",
                    "new_count",
                    "lines",
                ],
                "properties": {
                    "old_start": {"type": "integer", "minimum": 0},
                    "old_count": {"type": "integer", "minimum": 0},
                    "new_start": {"type": "integer", "minimum": 0},
                    "new_count": {"type": "integer", "minimum": 0},
                    "lines": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "required": ["type", "text"],
                            "properties": {
                                "type": {
                                    "enum": ["context", "add", "remove"],
                                },
                                "text": {"type": "string"},
                            },
                        },
                    },
                },
            },
        },
        "rationale": {"type": "string"},
        "evidence": {"type": "object"},
        "status": {"const": "proposed"},
        "requires_human_review": {"const": True},
        "auto_apply": {"const": False},
    },
}


# ── Data model ────────────────────────────────────────────────────────────
@dataclass
class BackoffSpec:
    """A backoff schedule to apply to the retry-policy surface."""

    base_delay: float
    max_delay: float = 120.0
    jitter_ratio: float = 0.5

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "BackoffSpec":
        return cls(
            base_delay=float(d.get("base_delay", 0.0)),
            max_delay=float(d.get("max_delay", 120.0)),
            jitter_ratio=float(d.get("jitter_ratio", 0.5)),
        )


@dataclass
class RetryPolicyChange:
    """A requested change to the retry-policy surface of a harness code block.

    ``before`` is the current source block (e.g. a function body). The
    generator synthesizes ``after`` by applying the requested surface changes
    (``retry_count`` / ``backoff`` / ``guard_condition``) to ``before``. Any
    surface left ``None`` is left unchanged.
    """

    target_file: str
    symbol: str
    before: str
    retry_count: Optional[int] = None
    backoff: Optional[BackoffSpec] = None
    guard_condition: Optional[str] = None
    rationale: str = ""


@dataclass
class CodeDiffProposal:
    """A structured code-diff proposal against the retry-policy surface."""

    target_file: str
    symbol: str
    before: str
    after: str
    retry_policy: Dict[str, Any]
    diff_delta: List[Dict[str, Any]]
    rationale: str = ""
    evidence: Dict[str, Any] = field(default_factory=dict)
    warnings: List[str] = field(default_factory=list)
    # --- HARD human-gating invariant: inert, never auto-applied. ---
    status: str = "proposed"
    requires_human_review: bool = True
    auto_apply: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "type": "retry_policy_code_diff",
            "source": "self-harness",
            "target_file": self.target_file,
            "symbol": self.symbol,
            "before": self.before,
            "after": self.after,
            "retry_policy": self.retry_policy,
            "diff_delta": self.diff_delta,
            "rationale": self.rationale,
            "evidence": self.evidence,
            "warnings": self.warnings,
            "status": self.status,
            "requires_human_review": self.requires_human_review,
            "auto_apply": self.auto_apply,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "CodeDiffProposal":
        return cls(
            target_file=str(d.get("target_file", "")),
            symbol=str(d.get("symbol", "")),
            before=str(d.get("before", "")),
            after=str(d.get("after", "")),
            retry_policy=dict(d.get("retry_policy", {}) or {}),
            diff_delta=list(d.get("diff_delta", []) or []),
            rationale=str(d.get("rationale", "")),
            evidence=dict(d.get("evidence", {}) or {}),
            warnings=list(d.get("warnings", []) or []),
            status=str(d.get("status", "proposed")),
            requires_human_review=bool(d.get("requires_human_review", True)),
            auto_apply=bool(d.get("auto_apply", False)),
        )


# ── Diff delta (line hunks) ────────────────────────────────────────────────
def _lcs(a: Sequence[str], b: Sequence[str]) -> List[Tuple[int, int]]:
    """Longest-common-subsequence of two line sequences.

    Returns a list of ``(i, j)`` index pairs (0-based) of matched lines. Used
    to align ``before`` and ``after`` so the diff delta is deterministic.
    """
    n, m = len(a), len(b)
    # DP table; O(n*m) is fine for the small code blocks this targets.
    dp = [[0] * (m + 1) for _ in range(n + 1)]
    for i in range(n - 1, -1, -1):
        for j in range(m - 1, -1, -1):
            if a[i] == b[j]:
                dp[i][j] = dp[i + 1][j + 1] + 1
            else:
                dp[i][j] = max(dp[i + 1][j], dp[i][j + 1])
    pairs: List[Tuple[int, int]] = []
    i = j = 0
    while i < n and j < m:
        if a[i] == b[j]:
            pairs.append((i, j))
            i += 1
            j += 1
        elif dp[i + 1][j] >= dp[i][j + 1]:
            i += 1
        else:
            j += 1
    return pairs


def diff_lines(before: str, after: str) -> List[Dict[str, Any]]:
    """Compute a structured diff delta between *before* and *after*.

    Returns a list of hunks, each::

        {
            "old_start": <1-based line in before>,
            "old_count": <lines in before covered>,
            "new_start": <1-based line in after>,
            "new_count": <lines in after covered>,
            "lines": [{"type": "context"|"add"|"remove", "text": <line>}, ...],
        }

    Deterministic (LCS-aligned, so matched lines are emitted as ``context``
    rather than spurious remove+add pairs), pure, stdlib-only. Empty when the
    blocks are identical.
    """
    a = (before or "").splitlines()
    b = (after or "").splitlines()
    if a == b:
        return []

    pairs = _lcs(a, b)
    hunks: List[Dict[str, Any]] = []
    current: Optional[Dict[str, Any]] = None
    i = j = 0

    def _flush() -> None:
        nonlocal current
        if current is not None:
            hunks.append(current)
            current = None

    for pi, pj in pairs:
        # Emit the change run between the previous match and this one.
        if i != pi or j != pj:
            if current is None:
                current = {
                    "old_start": i + 1,
                    "old_count": 0,
                    "new_start": j + 1,
                    "new_count": 0,
                    "lines": [],
                }
            for k in range(i, pi):
                current["lines"].append({"type": "remove", "text": a[k]})
                current["old_count"] += 1
            for k in range(j, pj):
                current["lines"].append({"type": "add", "text": b[k]})
                current["new_count"] += 1
        # The matched line is context (only emitted inside an open hunk).
        if current is not None:
            current["lines"].append({"type": "context", "text": a[pi]})
            current["old_count"] += 1
            current["new_count"] += 1
        i, j = pi + 1, pj + 1

    # Trailing changes after the last matched pair.
    if i < len(a) or j < len(b):
        if current is None:
            current = {
                "old_start": i + 1,
                "old_count": 0,
                "new_start": j + 1,
                "new_count": 0,
                "lines": [],
            }
        for k in range(i, len(a)):
            current["lines"].append({"type": "remove", "text": a[k]})
            current["old_count"] += 1
        for k in range(j, len(b)):
            current["lines"].append({"type": "add", "text": b[k]})
            current["new_count"] += 1

    _flush()
    return hunks


# ── Surface application ───────────────────────────────────────────────────
def _apply_retry_count(before: str, retry_count: int) -> Tuple[str, List[str]]:
    """Replace the retry-count surface in *before* with *retry_count*."""
    warnings: List[str] = []
    if retry_count < 0:
        warnings.append("retry_count < 0 ignored (must be >= 0)")
        return before, warnings
    new, n = _RETRY_COUNT_RE.subn(lambda m: f"{m.group('prefix')}{retry_count}", before)
    if n == 0:
        warnings.append(
            "no retry-count surface (max_retries/max_attempts) found in before block"
        )
    return new, warnings


def _apply_backoff(before: str, backoff: BackoffSpec) -> Tuple[str, List[str]]:
    """Replace the backoff surface in *before* with *backoff*."""
    warnings: List[str] = []
    new = before
    if backoff.base_delay < 0 or backoff.max_delay < 0 or backoff.jitter_ratio < 0:
        warnings.append("negative backoff value ignored")
        return before, warnings
    new, n = _BACKOFF_BASE_RE.subn(
        lambda m: f"{m.group('prefix')}{backoff.base_delay:g}", new
    )
    if n == 0:
        warnings.append("no base_delay surface found in before block")
    new, n2 = _BACKOFF_MAX_RE.subn(
        lambda m: f"{m.group('prefix')}{backoff.max_delay:g}", new
    )
    if n2 == 0:
        warnings.append("no max_delay surface found in before block")
    return new, warnings


def _apply_guard_condition(before: str, guard_condition: str) -> Tuple[str, List[str]]:
    """Replace the first ``if <cond>:`` guard in *before* with *guard_condition*."""
    warnings: List[str] = []
    cond = (guard_condition or "").strip()
    if not cond:
        warnings.append("empty guard_condition ignored")
        return before, warnings

    def _repl(m: re.Match) -> str:
        return f"{m.group('indent')}if {cond}:"

    new, n = _GUARD_RE.subn(_repl, before, count=1)
    if n == 0:
        warnings.append("no guard condition (if <cond>:) found in before block")
    return new, warnings


# ── Generator ─────────────────────────────────────────────────────────────
def generate_code_diff(change: RetryPolicyChange) -> CodeDiffProposal:
    """Generate a structured code-diff proposal from a retry-policy change.

    Synthesizes the ``after`` block by applying the requested surface changes
    (``retry_count`` / ``backoff`` / ``guard_condition``) to ``change.before``,
    computes the structured diff delta, and returns a :class:`CodeDiffProposal`.

    Deterministic and fail-safe: a requested surface that is not present in the
    ``before`` block is left unchanged and recorded in ``warnings`` — the
    generator never invents code. The proposal is inert data (``status`` =
    ``proposed``, ``requires_human_review`` = True, ``auto_apply`` = False);
    nothing is applied here.
    """
    warnings: List[str] = []
    after = change.before

    if change.retry_count is not None:
        after, w = _apply_retry_count(after, change.retry_count)
        warnings.extend(w)
    if change.backoff is not None:
        after, w = _apply_backoff(after, change.backoff)
        warnings.extend(w)
    if change.guard_condition is not None:
        after, w = _apply_guard_condition(after, change.guard_condition)
        warnings.extend(w)

    retry_policy: Dict[str, Any] = {
        "retry_count": change.retry_count,
        "backoff": change.backoff.to_dict() if change.backoff else None,
        "guard_condition": change.guard_condition,
    }

    return CodeDiffProposal(
        target_file=change.target_file,
        symbol=change.symbol,
        before=change.before,
        after=after,
        retry_policy=retry_policy,
        diff_delta=diff_lines(change.before, after),
        rationale=change.rationale,
        warnings=warnings,
    )


# ── Validation ────────────────────────────────────────────────────────────
def _check_type(value: Any, expected: str, path: str, errors: List[str]) -> None:
    """Append an error to *errors* if *value* is not of the expected JSON type."""
    if expected == "string":
        ok = isinstance(value, str)
    elif expected == "integer":
        ok = isinstance(value, int) and not isinstance(value, bool)
    elif expected == "number":
        ok = isinstance(value, (int, float)) and not isinstance(value, bool)
    elif expected == "object":
        ok = isinstance(value, dict)
    elif expected == "array":
        ok = isinstance(value, list)
    elif expected == "null":
        ok = value is None
    else:  # pragma: no cover - defensive
        ok = True
    if not ok:
        errors.append(f"{path}: expected {expected}, got {type(value).__name__}")


def validate_proposal(proposal: Any) -> List[str]:
    """Validate a proposal dict against :data:`RETRY_POLICY_DIFF_SCHEMA`.

    Returns a list of human-readable error strings; an empty list means the
    proposal is schema-valid. Stdlib-only (no ``jsonschema`` dependency) — a
    focused structural check of the fields the schema declares required.
    """
    errors: List[str] = []
    if not isinstance(proposal, dict):
        return ["proposal: expected object"]

    required = [
        "type",
        "source",
        "target_file",
        "symbol",
        "before",
        "after",
        "retry_policy",
        "diff_delta",
        "status",
        "requires_human_review",
        "auto_apply",
    ]
    for key in required:
        if key not in proposal:
            errors.append(f"missing required field: {key}")

    if "type" in proposal and proposal["type"] != "retry_policy_code_diff":
        errors.append("type: must be 'retry_policy_code_diff'")
    if "source" in proposal and proposal["source"] != "self-harness":
        errors.append("source: must be 'self-harness'")
    if "status" in proposal and proposal["status"] != "proposed":
        errors.append("status: must be 'proposed'")
    if (
        "requires_human_review" in proposal
        and proposal["requires_human_review"] is not True
    ):
        errors.append("requires_human_review: must be true")
    if "auto_apply" in proposal and proposal["auto_apply"] is not False:
        errors.append("auto_apply: must be false")

    for key in ("target_file", "symbol", "before", "after"):
        if key in proposal:
            _check_type(proposal[key], "string", key, errors)
            if isinstance(proposal[key], str) and not proposal[key]:
                errors.append(f"{key}: must be a non-empty string")

    rp = proposal.get("retry_policy")
    if rp is not None:
        _check_type(rp, "object", "retry_policy", errors)
        if isinstance(rp, dict):
            if "retry_count" in rp and rp["retry_count"] is not None:
                _check_type(
                    rp["retry_count"], "integer", "retry_policy.retry_count", errors
                )
                if isinstance(rp["retry_count"], int) and rp["retry_count"] < 0:
                    errors.append("retry_policy.retry_count: must be >= 0")
            if "guard_condition" in rp and rp["guard_condition"] is not None:
                _check_type(
                    rp["guard_condition"],
                    "string",
                    "retry_policy.guard_condition",
                    errors,
                )
            bo = rp.get("backoff")
            if bo is not None:
                _check_type(bo, "object", "retry_policy.backoff", errors)
                if isinstance(bo, dict):
                    if "base_delay" not in bo:
                        errors.append(
                            "retry_policy.backoff: missing required field base_delay"
                        )
                    else:
                        _check_type(
                            bo["base_delay"],
                            "number",
                            "retry_policy.backoff.base_delay",
                            errors,
                        )

    dd = proposal.get("diff_delta")
    if dd is not None:
        _check_type(dd, "array", "diff_delta", errors)
        if isinstance(dd, list):
            for idx, hunk in enumerate(dd):
                if not isinstance(hunk, dict):
                    errors.append(f"diff_delta[{idx}]: expected object")
                    continue
                for key in ("old_start", "old_count", "new_start", "new_count"):
                    if key not in hunk:
                        errors.append(
                            f"diff_delta[{idx}]: missing required field {key}"
                        )
                    else:
                        _check_type(
                            hunk[key], "integer", f"diff_delta[{idx}].{key}", errors
                        )
                if "lines" not in hunk:
                    errors.append(f"diff_delta[{idx}]: missing required field lines")
                elif isinstance(hunk.get("lines"), list):
                    for lidx, line in enumerate(hunk["lines"]):
                        if not isinstance(line, dict):
                            errors.append(
                                f"diff_delta[{idx}].lines[{lidx}]: expected object"
                            )
                            continue
                        if "type" not in line:
                            errors.append(
                                f"diff_delta[{idx}].lines[{lidx}]: missing type"
                            )
                        elif line["type"] not in ("context", "add", "remove"):
                            errors.append(
                                f"diff_delta[{idx}].lines[{lidx}].type: invalid"
                            )
                        if "text" not in line:
                            errors.append(
                                f"diff_delta[{idx}].lines[{lidx}]: missing text"
                            )
                        else:
                            _check_type(
                                line["text"],
                                "string",
                                f"diff_delta[{idx}].lines[{lidx}].text",
                                errors,
                            )

    return errors
