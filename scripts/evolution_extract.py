#!/usr/bin/env python3
"""Structured-draft validator for the evolution paper-to-capability extractor (#322).

The `evolution-extract` skill reads ONE high-value paper/finding surfaced by
`evolution-research` and distills the *concrete technique* (not just the idea)
into a small structured draft the rest of the pipeline can act on, following
SPRING (arXiv:2405.14980 — agents read papers, extract concrete strategies, and
apply them) rather than stopping at a recommendation report.

The hard, repeatable part of that stage is NOT the LLM reasoning — it is keeping
the draft WELL-FORMED so the downstream issues/analysis stages get a stable
contract instead of free-form prose (the same reason `evolution_evaluator` makes
the loop's scoring deterministic and `evolution_skill_lint` makes wiring a
mechanical gate). So this module is the deterministic referee for the draft:

  * a fixed required schema — ``technique``, ``expected_behavior_change``,
    ``testable_hypothesis``, ``source`` — each a non-empty string,
  * a `source` that must be a real reference (URL / arXiv id / DOI), not a vague
    "a paper" — a draft with no traceable origin cannot be A/B tested or audited,
  * a light injection screen on every field (drafts are distilled from UNTRUSTED
    paper text; a field that smuggles in "ignore previous instructions" / a fake
    `system:` turn / hidden zero-width text is rejected, not passed downstream),
  * normalization (trim, collapse whitespace) so cosmetic noise doesn't reach the
    issue body.

The LLM still does the open-ended work (read the paper, author the technique +
hypothesis). This module is the small deterministic gate the skill calls to
prove the draft is shippable. Pure functions + a thin CLI mirror the other
``scripts/evolution_*.py`` helpers so it is import-safe and unit-testable, and
the CLI gives the skill's terminal toolset a real call site (no dead code).

CLI (the skill calls this from its terminal tool with the draft JSON on stdin or
a path):

    evolution_extract.py validate draft.json
    cat draft.json | evolution_extract.py validate

It prints one JSON object: ``{"valid", "errors", "draft"}`` (``draft`` is the
NORMALIZED draft when valid, else null) and exits 0 when the draft is valid, 1
when it is well-formed JSON but fails validation, and 2 on bad input (not JSON /
unreadable / unknown action). The distinct exit codes let a shell gate branch
without parsing the JSON.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# The draft contract. Every field is REQUIRED and must be a non-empty string.
# Order is the canonical field order used when re-emitting the normalized draft.
REQUIRED_FIELDS: Tuple[str, ...] = (
    "technique",              # the concrete strategy/prompting/reasoning pattern
    "expected_behavior_change",  # how the agent should behave differently
    "testable_hypothesis",    # a falsifiable prediction an A/B test could check
    "source",                 # a traceable origin (URL / arXiv id / DOI)
)

# A field shorter than this (after normalization) is too thin to be a real
# technique/hypothesis — it's a placeholder, not a draft. `source` is exempt
# (an arXiv id like "2405.14980" is short but valid; it has its own check).
_MIN_FIELD_LEN = 12

# Injection markers. Drafts are distilled from UNTRUSTED paper text, and this
# chain (extract -> issues -> analysis -> implementation) reaches code, so a
# field carrying an instruction-shaped payload is rejected rather than passed on.
# Deliberately narrow + anchored to known attack shapes to avoid false positives
# on legitimate technical prose.
_INJECTION_PATTERNS: Tuple[re.Pattern, ...] = (
    re.compile(r"ignore\s+(?:all\s+)?previous\s+instructions", re.IGNORECASE),
    re.compile(r"disregard\s+(?:all\s+)?(?:previous|prior|above)", re.IGNORECASE),
    re.compile(r"^\s*(?:system|assistant|developer)\s*:", re.IGNORECASE | re.MULTILINE),
    re.compile(r"<\s*/?\s*(?:system|assistant|tool)\s*>", re.IGNORECASE),
)

# Zero-width / bidi-control characters used to hide text from a human reviewer.
_HIDDEN_CHARS_RE = re.compile(r"[​‌‍⁠﻿‪-‮]")

# A `source` is "traceable" if it carries a URL, an arXiv id, or a DOI. A bare
# "a recent paper" with no locator can't be A/B tested or audited — reject it.
_SOURCE_TRACEABLE_RE = re.compile(
    r"https?://\S+"                       # any URL
    r"|arxiv[:/]?\s*\d{4}\.\d{4,5}"       # arXiv:2405.14980 / arxiv/2405.14980
    r"|(?<!\d)\d{4}\.\d{4,5}(?:v\d+)?(?!\d)"  # bare arXiv id 2405.14980 / ...v2
    r"|10\.\d{4,9}/\S+",                  # DOI
    re.IGNORECASE,
)

_WHITESPACE_RE = re.compile(r"\s+")


def normalize_field(value: str) -> str:
    """Trim and collapse internal whitespace so cosmetic noise (line wraps,
    double spaces copied from a PDF) doesn't reach the issue body. Newlines and
    runs of spaces/tabs collapse to a single space; leading/trailing stripped."""
    return _WHITESPACE_RE.sub(" ", str(value)).strip()


def has_hidden_chars(value: str) -> bool:
    """True if the text contains zero-width or bidi-control characters."""
    return bool(_HIDDEN_CHARS_RE.search(value or ""))


def looks_like_injection(value: str) -> bool:
    """True if the text carries an instruction-injection-shaped payload."""
    return any(p.search(value or "") for p in _INJECTION_PATTERNS)


def source_is_traceable(value: str) -> bool:
    """True if `source` carries a URL / arXiv id / DOI (a real locator)."""
    return bool(_SOURCE_TRACEABLE_RE.search(value or ""))


def validate_draft(draft: Any) -> Tuple[bool, List[str], Optional[Dict[str, str]]]:
    """Validate ONE technique draft against the contract.

    Returns ``(valid, errors, normalized)``: ``errors`` is a list of
    human-readable reason strings (empty iff valid); ``normalized`` is the draft
    with every field trimmed/whitespace-collapsed and keys in canonical order
    (only when valid, else ``None``). Pure — no IO, safe to unit test.
    """
    errors: List[str] = []

    if not isinstance(draft, dict):
        return False, ["draft must be a JSON object"], None

    # Unknown keys are a smell (the author drifted from the contract) but not
    # fatal — flag them, keep validating, and drop them from the normalized form.
    extra = [k for k in draft if k not in REQUIRED_FIELDS]
    if extra:
        errors.append(f"unexpected field(s): {sorted(extra)}")

    normalized: Dict[str, str] = {}
    for field in REQUIRED_FIELDS:
        if field not in draft:
            errors.append(f"missing required field: '{field}'")
            continue
        raw = draft[field]
        if not isinstance(raw, str):
            errors.append(f"field '{field}' must be a string, got {type(raw).__name__}")
            continue
        if has_hidden_chars(raw):
            errors.append(f"field '{field}' contains hidden/zero-width characters")
            continue
        if looks_like_injection(raw):
            errors.append(f"field '{field}' contains instruction-injection-shaped text")
            continue
        value = normalize_field(raw)
        if not value:
            errors.append(f"field '{field}' is empty")
            continue
        if field != "source" and len(value) < _MIN_FIELD_LEN:
            errors.append(
                f"field '{field}' is too short ({len(value)} < {_MIN_FIELD_LEN} chars) — "
                f"give a concrete technique/hypothesis, not a placeholder"
            )
            continue
        if field == "source" and not source_is_traceable(value):
            errors.append(
                "field 'source' is not traceable — give a URL, arXiv id "
                "(e.g. arXiv:2405.14980), or DOI so the technique can be audited and A/B tested"
            )
            continue
        normalized[field] = value

    valid = not errors
    return valid, errors, (normalized if valid else None)


# ── IO boundary / CLI ────────────────────────────────────────────────────────
EXIT_VALID = 0
EXIT_INVALID = 1
EXIT_BAD_INPUT = 2


def _load_draft(path: Optional[str]) -> Tuple[Optional[Any], Optional[str]]:
    """Read the draft JSON from a positional path or stdin."""
    try:
        raw = Path(path).read_text(encoding="utf-8") if path else sys.stdin.read()
    except OSError as exc:
        return None, f"cannot read input: {exc}"
    try:
        return json.loads(raw), None
    except ValueError as exc:
        return None, f"input is not valid JSON: {exc}"


def main(argv: List[str]) -> int:
    if len(argv) < 2 or argv[1] != "validate":
        print("usage: evolution_extract.py validate [draft.json]", file=sys.stderr)
        return EXIT_BAD_INPUT
    path = argv[2] if len(argv) > 2 else None
    draft, load_err = _load_draft(path)
    if load_err:
        print(f"[evolution-extract] {load_err}", file=sys.stderr)
        return EXIT_BAD_INPUT
    valid, errors, normalized = validate_draft(draft)
    print(json.dumps({"valid": valid, "errors": errors, "draft": normalized}, sort_keys=True))
    return EXIT_VALID if valid else EXIT_INVALID


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
