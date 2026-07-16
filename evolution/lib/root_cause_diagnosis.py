# -*- coding: utf-8 -*-
"""Structured root-cause diagnosis for tool failures.

Implements GitHub issue #1026 — a child of #1019 (PALADIN-style
execution-level tool-failure recovery).  This module provides structured
diagnosis of *why* a tool call failed so that the recovery system can take
targeted corrective action instead of blindly retrying.

The pipeline is:

1. **Classify** the error message into one of eight :class:`FailureCategory`
   values using keyword / pattern matching.
2. **Diagnose** the root cause using the category, the tool name, and the
   surrounding context.
3. **Suggest fixes** appropriate to the category and tool.
4. **Score confidence** — more matching patterns yield higher confidence.
5. **Track** diagnoses over time to detect recurring failure patterns for a
   given tool.

Components:
    * :class:`FailureCategory` — enum of failure classification categories.
    * :class:`Diagnosis` — structured diagnosis record.
    * :class:`ErrorClassifier` — pattern-matching error classifier.
    * :class:`RootCauseAnalyzer` — orchestrates classification → diagnosis.
    * :class:`DiagnosisHistory` — per-tool diagnosis history with recurring-
      failure detection and statistics.

Design goals (matching the existing module style):
    * Pure functions + dataclasses; **no side effects on import**.
    * Full type hints, ``from __future__ import annotations``.
    * JSON serialization (``to_dict`` / ``from_dict``) for every dataclass.
    * Thread-safe shared state via ``threading.Lock`` where needed.
    * **No external dependencies** — standard library only.

Author: Hermes Evolution Implementer
Issue: #1026 (child of #1019 — PALADIN-style tool-failure recovery)
"""

from __future__ import annotations

import enum
import re
import threading
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

__version__ = "1.0.0"

__all__ = [
    "FailureCategory",
    "Diagnosis",
    "ErrorClassifier",
    "RootCauseAnalyzer",
    "DiagnosisHistory",
]


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------

class FailureCategory(enum.Enum):
    """Classification of a tool failure into a broad root-cause category.

    * ``NETWORK`` — connectivity / DNS / unreachable errors.
    * ``PERMISSION`` — authorization, 403, access-denied errors.
    * ``NOT_FOUND`` — 404, missing resource, ``No such file`` errors.
    * ``VALIDATION`` — schema mismatch, invalid input errors.
    * ``TIMEOUT`` — request timed out.
    * ``RESOURCE_LIMIT`` — rate limits, quotas, out-of-memory.
    * ``SYNTAX_ERROR`` — parse errors, malformed expressions.
    * ``UNKNOWN`` — unclassified failures.
    """

    NETWORK = "network"
    PERMISSION = "permission"
    NOT_FOUND = "not_found"
    VALIDATION = "validation"
    TIMEOUT = "timeout"
    RESOURCE_LIMIT = "resource_limit"
    SYNTAX_ERROR = "syntax_error"
    UNKNOWN = "unknown"

    @classmethod
    def from_string(cls, s: str) -> "FailureCategory":
        """Parse a :class:`FailureCategory` from a string (case-insensitive)."""
        s_lower = s.lower().strip()
        for fc in cls:
            if fc.value == s_lower:
                return fc
        raise ValueError(f"Unknown failure category: {s!r}")


# ---------------------------------------------------------------------------
# Data Classes
# ---------------------------------------------------------------------------

@dataclass
class Diagnosis:
    """A structured root-cause diagnosis for a tool failure.

    Attributes:
        category: The classified :class:`FailureCategory`.
        root_cause: A human-readable explanation of the primary cause.
        contributing_factors: Secondary factors that may have contributed.
        suggested_fixes: Ordered list of suggested corrective actions.
        confidence: Confidence score in the range [0, 1].
        metadata: Arbitrary key-value metadata for extensibility.
    """

    category: FailureCategory = FailureCategory.UNKNOWN
    root_cause: str = ""
    contributing_factors: List[str] = field(default_factory=list)
    suggested_fixes: List[str] = field(default_factory=list)
    confidence: float = 0.0
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to a JSON-compatible dictionary."""
        return {
            "category": self.category.value,
            "root_cause": self.root_cause,
            "contributing_factors": list(self.contributing_factors),
            "suggested_fixes": list(self.suggested_fixes),
            "confidence": self.confidence,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Diagnosis":
        """Deserialize from a dictionary produced by :meth:`to_dict`."""
        return cls(
            category=FailureCategory.from_string(d["category"])
            if isinstance(d.get("category"), str)
            else d.get("category", FailureCategory.UNKNOWN),
            root_cause=d.get("root_cause", ""),
            contributing_factors=list(d.get("contributing_factors", [])),
            suggested_fixes=list(d.get("suggested_fixes", [])),
            confidence=float(d.get("confidence", 0.0)),
            metadata=dict(d.get("metadata", {})),
        )

    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        return (
            f"Diagnosis(category={self.category.value!r}, "
            f"confidence={self.confidence:.2f}, "
            f"root_cause={self.root_cause!r})"
        )


# ---------------------------------------------------------------------------
# Error Classifier
# ---------------------------------------------------------------------------

class ErrorClassifier:
    """Classifies error messages into :class:`FailureCategory` values.

    Uses case-insensitive keyword / substring matching.  Categories are
    checked in a priority order designed to be as specific as possible —
    e.g. ``timeout`` wins over generic ``connection`` errors.

    The classifier can be extended with custom patterns via
    :meth:`add_pattern`.
    """

    @staticmethod
    def _pattern_matches(pattern: str, text: str) -> bool:
        """Return True if *pattern* occurs in *text* (both already lower-cased).

        Short single-token patterns (``<= 4`` chars, no whitespace) — e.g. HTTP
        status codes like ``"404"`` or short words like ``"host"`` — are matched
        with alphanumeric boundaries so they do not spuriously fire when
        embedded inside a larger token (``"400"`` inside ``"4001"``, ``"host"``
        inside ``"ghost"``).  Longer or multi-word patterns keep plain
        substring semantics.
        """
        if len(pattern) <= 4 and " " not in pattern:
            return (
                re.search(
                    r"(?<![0-9a-z])" + re.escape(pattern) + r"(?![0-9a-z])",
                    text,
                )
                is not None
            )
        return pattern in text

    # Default patterns per category, lower-cased substrings.
    _DEFAULT_PATTERNS: Dict[FailureCategory, List[str]] = {
        FailureCategory.TIMEOUT: [
            "timeout",
            "timed out",
            "deadline exceeded",
        ],
        FailureCategory.PERMISSION: [
            "permission denied",
            "403",
            "forbidden",
            "unauthorized",
            "access denied",
            "not authorized",
        ],
        FailureCategory.NOT_FOUND: [
            "not found",
            "404",
            "no such file",
            "does not exist",
            "not exist",
        ],
        FailureCategory.RESOURCE_LIMIT: [
            "rate limit",
            "429",
            "quota",
            "too many requests",
            "out of memory",
            "resource exhausted",
        ],
        FailureCategory.NETWORK: [
            "connection",
            "network",
            "unreachable",
            "refused",
            "dns",
            "host",
            "reset by peer",
        ],
        FailureCategory.VALIDATION: [
            "validation",
            "invalid",
            "schema",
            "bad request",
            "400",
            "malformed",
            "unexpected argument",
            "unexpected keyword",
            "missing required",
        ],
        FailureCategory.SYNTAX_ERROR: [
            "syntax",
            "parse error",
            "syntaxerror",
            "indentationerror",
            "unexpected token",
            "unexpected eof",
        ],
    }

    def __init__(
        self,
        patterns: Optional[Dict[FailureCategory, List[str]]] = None,
    ) -> None:
        """Initialize with optional custom patterns; defaults are used otherwise."""
        if patterns is not None:
            self._patterns: Dict[FailureCategory, List[str]] = {
                cat: [p.lower() for p in pats]
                for cat, pats in patterns.items()
            }
        else:
            # Deep-copy defaults so mutations don't affect the class default.
            self._patterns = {
                cat: list(pats)
                for cat, pats in self._DEFAULT_PATTERNS.items()
            }
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Classification
    # ------------------------------------------------------------------

    def classify(
        self,
        error_message: str,
        error_type: str = "",
    ) -> FailureCategory:
        """Classify an error message (and optional error type) into a category.

        The combined text of *error_message* and *error_type* is searched for
        known patterns.  Categories are checked in priority order (most
        specific first).  If nothing matches, :attr:`FailureCategory.UNKNOWN`
        is returned.

        Args:
            error_message: The primary error message string.
            error_type: Optional exception type name (e.g. ``"TimeoutError"``).

        Returns:
            The best-matching :class:`FailureCategory`.
        """
        if not error_message and not error_type:
            return FailureCategory.UNKNOWN

        combined = f"{error_message} {error_type}".lower()

        # Priority order: most specific → most generic.
        priority: List[FailureCategory] = [
            FailureCategory.TIMEOUT,
            FailureCategory.PERMISSION,
            FailureCategory.NOT_FOUND,
            FailureCategory.RESOURCE_LIMIT,
            FailureCategory.SYNTAX_ERROR,
            FailureCategory.VALIDATION,
            FailureCategory.NETWORK,
        ]
        with self._lock:
            for cat in priority:
                patterns = self._patterns.get(cat, [])
                for pat in patterns:
                    if self._pattern_matches(pat, combined):
                        return cat
        return FailureCategory.UNKNOWN

    # ------------------------------------------------------------------
    # Pattern management
    # ------------------------------------------------------------------

    def get_category_patterns(self) -> Dict[FailureCategory, List[str]]:
        """Return a copy of the current pattern map."""
        with self._lock:
            return {cat: list(pats) for cat, pats in self._patterns.items()}

    def add_pattern(self, category: FailureCategory, pattern: str) -> None:
        """Add a custom pattern (substring) for a category."""
        with self._lock:
            self._patterns.setdefault(category, [])
            if pattern.lower() not in self._patterns[category]:
                self._patterns[category].append(pattern.lower())

    def count_matches(
        self,
        error_message: str,
        error_type: str = "",
    ) -> Dict[FailureCategory, int]:
        """Count how many patterns match for each category.

        Used internally for confidence scoring; exposed for testing.
        """
        combined = f"{error_message} {error_type}".lower()
        counts: Dict[FailureCategory, int] = {}
        with self._lock:
            for cat, patterns in self._patterns.items():
                c = sum(1 for p in patterns if self._pattern_matches(p, combined))
                if c:
                    counts[cat] = c
        return counts


# ---------------------------------------------------------------------------
# Root Cause Analyzer
# ---------------------------------------------------------------------------

class RootCauseAnalyzer:
    """Orchestrates classification, root-cause determination, and fix suggestion.

    Usage::

        analyzer = RootCauseAnalyzer()
        diag = analyzer.analyze(
            "search_files",
            "Connection refused while reaching index server",
            "ConnectionError",
            {"retry_count": 2},
        )
        print(diag.category)    # FailureCategory.NETWORK
        print(diag.suggested_fixes)
    """

    def __init__(self, classifier: Optional[ErrorClassifier] = None) -> None:
        self._classifier = classifier or ErrorClassifier()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def analyze(
        self,
        tool_name: str,
        error_message: str,
        error_type: str = "",
        context: Optional[Dict[str, Any]] = None,
    ) -> Diagnosis:
        """Produce a full :class:`Diagnosis` for a tool failure.

        Args:
            tool_name: Name of the tool that failed.
            error_message: The error message.
            error_type: Optional exception class name.
            context: Optional context dict (e.g. retry count, args).

        Returns:
            A populated :class:`Diagnosis`.
        """
        context = dict(context) if context else {}
        category = self._classifier.classify(error_message, error_type)
        root_cause = self._determine_root_cause(
            category, tool_name, error_message, context
        )
        contributing = self._generate_contributing_factors(
            category, tool_name, error_message, context
        )
        fixes = self._generate_fixes(category, tool_name, error_message)
        confidence = self._compute_confidence(category, error_message, error_type)

        return Diagnosis(
            category=category,
            root_cause=root_cause,
            contributing_factors=contributing,
            suggested_fixes=fixes,
            confidence=confidence,
            metadata={
                "tool_name": tool_name,
                "error_type": error_type,
                "context": context,
            },
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _determine_root_cause(
        self,
        category: FailureCategory,
        tool_name: str,
        error_message: str,
        context: Dict[str, Any],
    ) -> str:
        """Determine a human-readable root cause string."""
        causes = {
            FailureCategory.NETWORK: (
                f"A network-level failure occurred while calling '{tool_name}'. "
                f"The tool could not reach its target service or endpoint."
            ),
            FailureCategory.PERMISSION: (
                f"'{tool_name}' was denied access due to insufficient permissions "
                f"or authentication failure."
            ),
            FailureCategory.NOT_FOUND: (
                f"'{tool_name}' referenced a resource that does not exist."
            ),
            FailureCategory.VALIDATION: (
                f"'{tool_name}' received input that failed validation against "
                f"its expected schema."
            ),
            FailureCategory.TIMEOUT: (
                f"'{tool_name}' exceeded the maximum allowed execution time."
            ),
            FailureCategory.RESOURCE_LIMIT: (
                f"'{tool_name}' hit a resource limit (rate limit, quota, or "
                f"capacity constraint)."
            ),
            FailureCategory.SYNTAX_ERROR: (
                f"'{tool_name}' encountered a syntax or parse error in the "
                f"provided input or code."
            ),
            FailureCategory.UNKNOWN: (
                f"'{tool_name}' failed for an unclassified reason. "
                f"Manual investigation may be needed."
            ),
        }
        base = causes.get(category, causes[FailureCategory.UNKNOWN])
        # Enrich with context clues.
        if context.get("retry_count", 0) and category in (
            FailureCategory.TIMEOUT,
            FailureCategory.NETWORK,
            FailureCategory.RESOURCE_LIMIT,
        ):
            base += (
                f" This failure persisted across {context['retry_count']} "
                f"previous attempt(s), suggesting a non-transient condition."
            )
        return base

    def _generate_contributing_factors(
        self,
        category: FailureCategory,
        tool_name: str,
        error_message: str,
        context: Dict[str, Any],
    ) -> List[str]:
        """Generate a list of contributing factors based on category and context."""
        factors: List[str] = []
        retry_count = context.get("retry_count", 0)
        if retry_count:
            factors.append(
                f"Failure persisted after {retry_count} retry attempt(s)."
            )
        args = context.get("args")
        if args:
            factors.append(f"Arguments at time of failure: {args}")
        if category == FailureCategory.NETWORK:
            factors.append("Network connectivity or DNS resolution issues.")
            if context.get("endpoint"):
                factors.append(f"Target endpoint: {context['endpoint']}")
        elif category == FailureCategory.PERMISSION:
            factors.append("Missing or expired credentials / token.")
            factors.append("Insufficient role or scope for the operation.")
        elif category == FailureCategory.NOT_FOUND:
            factors.append("Resource may have been deleted or renamed.")
            if context.get("path"):
                factors.append(f"Referenced path: {context['path']}")
        elif category == FailureCategory.VALIDATION:
            factors.append("Input arguments do not match the tool's schema.")
            if context.get("schema_errors"):
                factors.append(f"Schema errors: {context['schema_errors']}")
        elif category == FailureCategory.TIMEOUT:
            factors.append("Operation is slower than expected or hung.")
            factors.append("Timeout threshold may be set too low.")
        elif category == FailureCategory.RESOURCE_LIMIT:
            factors.append("Rate-limit or quota policy enforced by the provider.")
            if context.get("retry_after"):
                factors.append(f"Retry after: {context['retry_after']}s")
        elif category == FailureCategory.SYNTAX_ERROR:
            factors.append("Malformed input expression or code snippet.")
            if context.get("line_number"):
                factors.append(f"Error near line {context['line_number']}")
        elif category == FailureCategory.UNKNOWN:
            factors.append("Insufficient information for detailed analysis.")
        return factors

    def _generate_fixes(
        self,
        category: FailureCategory,
        tool_name: str,
        error_message: str,
    ) -> List[str]:
        """Generate per-category suggested fixes."""
        fixes = {
            FailureCategory.NETWORK: [
                f"Retry '{tool_name}' after verifying network connectivity.",
                "Check DNS resolution and firewall rules for the target host.",
                "If using a proxy, verify proxy configuration is correct.",
                "Consider using a different endpoint or retrying with backoff.",
            ],
            FailureCategory.PERMISSION: [
                f"Verify that valid credentials are configured for '{tool_name}'.",
                "Refresh or re-authenticate the current token / session.",
                "Check that the current user or service account has the required role/scope.",
                "Request elevated permissions if this operation requires them.",
            ],
            FailureCategory.NOT_FOUND: [
                f"Verify the resource referenced by '{tool_name}' still exists.",
                "Check for typos in identifiers, paths, or names.",
                "List available resources to confirm the correct identifier.",
                "If the resource was recently deleted, recreate it or choose an alternative.",
            ],
            FailureCategory.VALIDATION: [
                f"Review the arguments passed to '{tool_name}' against its schema.",
                "Ensure all required parameters are provided and correctly typed.",
                "Remove unexpected or unsupported arguments.",
                "Consult the tool documentation for valid input formats.",
            ],
            FailureCategory.TIMEOUT: [
                f"Increase the timeout threshold for '{tool_name}' if possible.",
                "Reduce the scope or size of the request.",
                "Break the operation into smaller batches.",
                "Retry with exponential backoff.",
            ],
            FailureCategory.RESOURCE_LIMIT: [
                f"Reduce request frequency to stay within rate limits for '{tool_name}'.",
                "Wait for the quota window to reset before retrying.",
                "Upgrade to a higher tier plan if persistent limits are hit.",
                "Implement client-side throttling or request queuing.",
            ],
            FailureCategory.SYNTAX_ERROR: [
                f"Check the input to '{tool_name}' for syntax correctness.",
                "Validate the expression or code against its grammar.",
                "Use a linter or formatter to identify the error location.",
                "Refer to the language or tool syntax documentation.",
            ],
            FailureCategory.UNKNOWN: [
                f"Inspect the full error output from '{tool_name}' for clues.",
                f"Try running '{tool_name}' with verbose/debug logging enabled.",
                "Search for known issues related to this error message.",
                "Escalate to a human if the failure persists.",
            ],
        }
        return list(fixes.get(category, fixes[FailureCategory.UNKNOWN]))

    def _compute_confidence(
        self,
        category: FailureCategory,
        error_message: str,
        error_type: str = "",
    ) -> float:
        """Compute a confidence score in [0, 1].

        Higher when more patterns match for the winning category.  A baseline
        of 0.3 is given for any classified category (at least one match),
        rising toward 1.0 as more distinct patterns match.
        """
        if category == FailureCategory.UNKNOWN:
            return 0.1
        match_counts = self._classifier.count_matches(
            error_message, error_type
        )
        winning_count = match_counts.get(category, 0)
        total_matches = sum(match_counts.values()) or 1
        # Base confidence: at least 0.3 for one match, up to ~0.9 for many.
        base = 0.3 + 0.15 * min(winning_count, 4)
        # If the winning category dominates (few cross-category matches),
        # boost slightly.
        dominance = winning_count / total_matches
        confidence = base + 0.1 * dominance
        return round(min(confidence, 1.0), 3)


# ---------------------------------------------------------------------------
# Diagnosis History
# ---------------------------------------------------------------------------

class DiagnosisHistory:
    """Tracks :class:`Diagnosis` records per tool with statistics.

    Thread-safe.  Maintains an ordered list of diagnoses per tool name.
    Supports retrieving recent diagnoses, detecting the most common recurring
    category, and computing aggregate statistics.
    """

    def __init__(self) -> None:
        self._history: Dict[str, List[Diagnosis]] = defaultdict(list)
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Mutators
    # ------------------------------------------------------------------

    def add(self, tool_name: str, diagnosis: Diagnosis) -> None:
        """Record a diagnosis for *tool_name*."""
        with self._lock:
            self._history[tool_name].append(diagnosis)

    def clear(self, tool_name: Optional[str] = None) -> None:
        """Clear history for a specific tool, or all tools if *None*."""
        with self._lock:
            if tool_name is None:
                self._history.clear()
            else:
                self._history.pop(tool_name, None)

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def get_recent(
        self,
        tool_name: str,
        n: int = 5,
    ) -> List[Diagnosis]:
        """Return the *n* most recent diagnoses for *tool_name*."""
        with self._lock:
            diagnoses = list(self._history.get(tool_name, []))
        if n >= len(diagnoses):
            return diagnoses
        return diagnoses[-n:] if n > 0 else []

    def get_recurring(
        self,
        tool_name: str,
    ) -> Optional[FailureCategory]:
        """Return the most common category for *tool_name*, or ``None``.

        A category is considered *recurring* if it appears more than once
        or is the sole diagnosis.  Returns ``None`` if no history exists.
        Ties are broken by the most recently occurring category.
        """
        with self._lock:
            diagnoses = list(self._history.get(tool_name, []))
        if not diagnoses:
            return None
        counts: Counter = Counter(d.category for d in diagnoses)
        max_count = counts.most_common(1)[0][1]
        # If only one entry, it's not truly "recurring" yet.
        if len(diagnoses) < 2:
            return None
        # Gather all categories tied for max count.
        tied = [cat for cat, c in counts.items() if c == max_count]
        if len(tied) == 1:
            return tied[0]
        # Tie-break: most recently occurring among tied.
        for d in reversed(diagnoses):
            if d.category in tied:
                return d.category
        return tied[0]

    def stats(self) -> Dict[str, Any]:
        """Return aggregate statistics across all tracked diagnoses.

        Returns:
            A dict with keys:
                * ``total_diagnoses`` — int
                * ``per_category`` — ``{category_value: count}``
                * ``per_tool`` — ``{tool_name: count}``
        """
        with self._lock:
            all_diags: List[Diagnosis] = []
            tool_counts: Dict[str, int] = {}
            for tool, diags in self._history.items():
                all_diags.extend(diags)
                tool_counts[tool] = len(diags)
        cat_counts: Counter = Counter(d.category for d in all_diags)
        return {
            "total_diagnoses": len(all_diags),
            "per_category": {
                cat.value: cat_counts.get(cat, 0) for cat in FailureCategory
            },
            "per_tool": tool_counts,
        }

    # ------------------------------------------------------------------
    # Serialization
    # ------------------------------------------------------------------

    def to_dict(self) -> Dict[str, Any]:
        """Serialize all history to a JSON-compatible dict."""
        with self._lock:
            return {
                tool: [d.to_dict() for d in diags]
                for tool, diags in self._history.items()
            }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "DiagnosisHistory":
        """Reconstruct a :class:`DiagnosisHistory` from :meth:`to_dict` output."""
        hist = cls()
        for tool, diag_dicts in d.items():
            for dd in diag_dicts:
                hist.add(tool, Diagnosis.from_dict(dd))
        return hist


# ---------------------------------------------------------------------------
# CLI (optional convenience)
# ---------------------------------------------------------------------------

if __name__ == "__main__":  # pragma: no cover
    import json
    import sys

    if len(sys.argv) < 3:
        print(
            "Usage: python root_cause_diagnosis.py <tool_name> <error_message> "
            "[error_type]"
        )
        sys.exit(1)

    _tool = sys.argv[1]
    _msg = sys.argv[2]
    _type = sys.argv[3] if len(sys.argv) > 3 else ""

    _analyzer = RootCauseAnalyzer()
    _diag = _analyzer.analyze(_tool, _msg, _type)
    print(json.dumps(_diag.to_dict(), indent=2))
