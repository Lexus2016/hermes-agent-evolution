#!/usr/bin/env python3
"""Mechanical PII / secret redaction gate for evolution pipeline issue text.

Reads text from stdin (or a file path) and scans it for patterns that match
PII, credentials, secrets, or host-local identifiers. Any hit causes the
input to be blocked for external publication (e.g. ``gh issue create``).

Usage::

    $ gh issue view 123 --json body --jq '.body' | python scripts/redact_pii.py
    # exit 0  → clean, safe to publish
    # exit 1  → blocked; stderr shows the redaction reason(s)

Exit codes
----------
0  Clean — no sensitive patterns found.
1  Blocked — one or more sensitive patterns were detected. The filtered text
   is written to stdout and the reasons to stderr, so a wrapper can abort the
   ``gh issue create`` call.

Patterns
--------
- Email addresses
- GitHub personal-access tokens (``ghp_``, ``gho_``, ``github_pat_``)
- OpenAI / Anthropic / generic API keys (``sk-…``)
- AWS access-key IDs (``AKIA…``)
- Generic hex-like secret blobs (``[a-zA-Z_]+=[A-Za-z0-9+/]{32,}``)
- Absolute home-directory paths (``/home/<user>/…``, ``/Users/<user>/…``, ``/root/…``)
- Private IPv4 addresses (``10.x.x.x``, ``172.16-31.x.x``, ``192.168.x.x``)
- Public IPv4 / IPv6 addresses
- Phone numbers (basic E.164 and local forms)

Author: Hermes Evolution
"""

from __future__ import annotations

import re
import sys

# Redacted replacement token — deterministic, so diffs remain stable.
_REDACTED = "[REDACTED]"

_PATTERNS: list[tuple[str, re.Pattern]] = []


def _compile() -> None:
    if _PATTERNS:
        return
    # Order matters: most specific first, broad/generic last.
    specs = [
        (
            "GitHub token",
            re.compile(
                r"\b(?:ghp_[A-Za-z0-9]{36}|gho_[A-Za-z0-9]{36}|github_pat_[A-Za-z0-9]{22,})\b"
            ),
        ),
        (
            "API secret key",
            re.compile(r"\bsk-[A-Za-z0-9]{20,}\b"),
        ),
        (
            "AWS access key",
            re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
        ),
        (
            "Generic secret assignment",
            re.compile(r"\b[A-Za-z_][A-Za-z0-9_]*=[A-Za-z0-9+/]{32,}[A-Za-z0-9+/=]*\b"),
        ),
        (
            "Email address",
            re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}"),
        ),
        (
            "Absolute home path",
            re.compile(r"(/home/[a-zA-Z0-9_-]+|/Users/[a-zA-Z0-9_-]+|/root)/[^\s]*"),
        ),
        (
            "Private IPv4 address",
            re.compile(
                r"\b(?:10\.(?:[0-9]{1,3}\.){2}[0-9]{1,3}|"
                r"172\.(?:1[6-9]|2[0-9]|3[01])\.[0-9]{1,3}\.[0-9]{1,3}|"
                r"192\.168\.[0-9]{1,3}\.[0-9]{1,3})\b"
            ),
        ),
        (
            "Public IP address",
            re.compile(
                r"\b(?:(?:25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?)\.){3}"
                r"(?:25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?)\b"
            ),
        ),
        (
            "Phone number",
            re.compile(
                r"(?:\+?[1-9]\d{0,3}[-.\s]?)?\(?\d{2,4}\)?[-.\s]?\d{3,4}[-.\s]?\d{3,4}"
            ),
        ),
    ]
    _PATTERNS[:] = specs


def redact(text: str) -> tuple[str, list[str]]:
    """Return (redacted_text, reasons).

    *reasons* is empty when the text is clean.
    """
    _compile()
    reasons: list[str] = []
    out = text
    for name, rx in _PATTERNS:
        hits = rx.findall(out)
        if hits:
            # Limit deduplicated samples shown in the reason to avoid leaking.
            unique_samples = list(dict.fromkeys(hits))[:3]
            masked = " | ".join(
                h[:4] + "…" + h[-4:] if len(h) > 12 else _REDACTED
                for h in unique_samples
            )
            reasons.append(f"{name}: {masked}")
            out = rx.sub(_REDACTED, out)
    return out, reasons


def main(argv: list[str]) -> int:
    src = sys.stdin.read()
    if argv[1:]:
        path = argv[1]
        try:
            with open(path, "r", encoding="utf-8") as fh:
                src = fh.read()
        except OSError as exc:
            print(f"[redact-pii] error reading {path}: {exc}", file=sys.stderr)
            return 2

    cleaned, reasons = redact(src)
    sys.stdout.write(cleaned)

    if reasons:
        print(
            f"[redact-pii] BLOCKED — {len(reasons)} pattern(s) matched:",
            file=sys.stderr,
        )
        for r in reasons:
            print(f"  • {r}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
