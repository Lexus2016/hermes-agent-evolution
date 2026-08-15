"""
Credential masking for tool/terminal output — issue #2435.

Prevents secrets (API keys, tokens, passwords, private keys, connection-string
passwords) from leaking into the agent's conversation context, logs, and
pipeline artifacts. Inspired by the credential-masking feature Google shipped
in Antigravity 2.0 (May 2026): tool output is passed through a regex-based
redaction filter BEFORE it enters the model's context.

Design constraints (see AGENTS.md):
* Pure function over text — no I/O, no network, no dependency on the event
  loop. Safe to call from any tool-result path.
* Idempotent: masking already-masked text is a no-op (``[REDACTED:…]`` markers
  contain no token-shaped substrings), so double-masking is harmless.
* Conservative patterns only: each regex matches a well-known, high-entropy
  credential FORMAT (AWS key ids, ``ghp_``/``sk-``/``xox`` token families,
  JWTs, PEM private-key blocks, ``scheme://user:password@host`` passwords).
  No fuzzy "looks like a secret" heuristics that would redact ordinary words.
* Opt-in via ``config.yaml`` (``security.credential_masking``), NOT a new
  ``HERMES_*`` env var — behavioral settings belong in config. Default is
  off so existing sessions and test fixtures that intentionally print fake
  tokens keep byte-identical output until the operator turns masking on.

Usage:
    from tools.credential_masking import mask_credentials
    safe = mask_credentials(raw_tool_output)
"""

from __future__ import annotations

import re
from typing import Iterable, List, Optional, Tuple

__all__ = [
    "mask_credentials",
    "has_credentials",
    "count_credentials",
    "masking_enabled",
    "CREDENTIAL_PATTERNS",
]


# ── Pattern table ───────────────────────────────────────────────────────
# Ordered: most specific first (e.g. Anthropic's ``sk-ant-`` before the
# generic ``sk-`` key; PEM blocks before any single-line rule could eat part
# of one). Each entry is (name, compiled_regex, replacement).
_REDACT = "[REDACTED:{name}]"

_CredentialPattern = Tuple[str, re.Pattern, str]


def _p(name: str, pattern: str, flags: int = 0) -> _CredentialPattern:
    return (name, re.compile(pattern, flags), _REDACT.format(name=name))


def _p_group(name: str, pattern: str, replacement: str, flags: int = 0) -> _CredentialPattern:
    return (name, re.compile(pattern, flags), replacement)


CREDENTIAL_PATTERNS: List[_CredentialPattern] = [
    # PEM private-key blocks (multi-line) — highest specificity, eats the
    # whole block including header/footer.
    _p("PRIVATE-KEY", r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY( BLOCK)?-----(?:.|\n)*?-----END [A-Z0-9 ]*PRIVATE KEY( BLOCK)?-----"),
    # JWTs (three base64url segments starting with eyJ…)
    _p("JWT", r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\b"),
    # GitHub tokens: ghp_ / gho_ / ghu_ / ghs_ / ghr_ + 36+ alphanumerics
    _p("GITHUB-TOKEN", r"\bgh[pousr]_[A-Za-z0-9]{36,255}\b"),
    # GitLab personal access tokens
    _p("GITLAB-TOKEN", r"\bglpat-[A-Za-z0-9_\-]{20,}\b"),
    # Slack tokens: xoxb- / xoxa- / xoxp- / xoxr- / xoxs-
    _p("SLACK-TOKEN", r"\bxox[baprs]-[A-Za-z0-9\-]{10,}\b"),
    # Anthropic API keys (before generic sk-)
    _p("ANTHROPIC-KEY", r"\bsk-ant-[A-Za-z0-9_\-]{16,}\b"),
    # OpenAI-style API keys (no dashes in the tail)
    _p("OPENAI-KEY", r"\bsk-(?:proj-)?[A-Za-z0-9]{20,}\b"),
    # Stripe live keys
    _p("STRIPE-KEY", r"\b[sr]k_live_[A-Za-z0-9]{16,}\b"),
    # AWS access key ids
    _p("AWS-KEY", r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    # Google API keys
    _p("GOOGLE-KEY", r"\bAIza[0-9A-Za-z_\-]{35}\b"),
    # Bearer authorization headers (keep the scheme, redact the credential)
    _p_group(
        "BEARER-TOKEN",
        r"(?i)(\bbearer[ \t]+)[A-Za-z0-9._\-+/=]{16,}",
        r"\g<1>[REDACTED:BEARER-TOKEN]",
    ),
    # Connection-string passwords: scheme://user:password@host — keep
    # scheme + user, redact only the password segment.
    _p_group(
        "URL-PASSWORD",
        r"(?i)\b([a-z][a-z0-9+.\-]*://)([^:/\s@]+):([^@\s/]+)@",
        r"\g<1>\g<2>:[REDACTED:URL-PASSWORD]@",
    ),
]


# ── Core API ────────────────────────────────────────────────────────────


def mask_credentials(
    text: Optional[str],
    extra_patterns: Iterable[_CredentialPattern] = (),
) -> str:
    """Return ``text`` with credential-shaped substrings replaced by markers.

    Every pattern in :data:`CREDENTIAL_PATTERNS` (plus any caller-supplied
    ``extra_patterns`` in the same ``(name, regex, replacement)`` shape) is
    applied in order. ``None`` input returns an empty string so callers can
    feed optional fields without guarding.

    Idempotent: ``[REDACTED:NAME]`` markers contain no token-shaped
    substring, so re-masking masked output is a no-op.
    """
    if not text:
        return ""
    for _name, regex, replacement in list(CREDENTIAL_PATTERNS) + list(extra_patterns):
        text = regex.sub(replacement, text)
    return text


def has_credentials(text: Optional[str]) -> bool:
    """True if ``text`` contains at least one credential-shaped substring."""
    if not text:
        return False
    return count_credentials(text) > 0


def count_credentials(text: Optional[str]) -> int:
    """Count credential-shaped substrings in ``text`` (per pattern, summed)."""
    if not text:
        return 0
    total = 0
    for _name, regex, _replacement in CREDENTIAL_PATTERNS:
        total += len(regex.findall(text))
    return total


# ── Config gate ─────────────────────────────────────────────────────────


def masking_enabled() -> bool:
    """Return True when output masking is enabled in ``config.yaml``.

    Gate: ``security.credential_masking`` (default ``False`` — opt-in).
    Masking rewrites every tool result the model sees; it must not change
    session bytes for existing users (or tests that deliberately print fake
    tokens) until the operator turns it on. Falls back to the default when
    config is missing or unreadable — a config read failure must never
    raise out of a tool-result path.
    """
    default = False
    try:
        from hermes_cli.config import load_config

        cfg = (load_config() or {}).get("security", {}) or {}
        return bool(cfg.get("credential_masking", default))
    except Exception:
        return default


def mask_tool_output(text: Optional[str]) -> str:
    """Choke-point helper: mask ``text`` only when the config gate is on.

    This is the single function tool-result paths should call — it keeps the
    enable/disable decision in one place so wiring stays trivial:

        from tools.credential_masking import mask_tool_output
        return mask_tool_output(raw_output)
    """
    if not masking_enabled():
        return text or ""
    return mask_credentials(text)
