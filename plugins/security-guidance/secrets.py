"""Secret detection for the security-guidance plugin (Hermes addition, #398).

Child of #390 — first shippable slice of the security code-review plugin.
NOT part of the Anthropic fork: ``patterns.py`` is byte-for-byte upstream, so
this Hermes-side logic lives in its own module. Two layers:

1. Regex rules for well-known credential formats (AWS, GitHub, Slack, Google,
   Stripe, npm, PEM private keys, JWT, generic api-key assignments).
2. A conservative Shannon-entropy check: a high-entropy value assigned to a
   secret-named key, with obvious placeholders/example values excluded. The
   threshold is deliberately conservative (~4.0 bits/char) to keep the
   false-positive rate low, so it will NOT flag low-entropy human passphrases
   (e.g. "correcthorsebatterystaple"); known-format keys are caught by layer 1.

Findings are returned as ``(ruleName, reminder)`` tuples — the same shape the
regex security rules use — so they flow through the existing warn/block path in
``__init__.py`` with no special handling.
"""

from __future__ import annotations

import math
import re
from typing import Dict, List, Set, Tuple

# Same scan cap as the regex scanner — pattern-matching a huge blob is poor
# signal-to-noise and slows the agent loop.
# Same scan cap as the regex scanner in __init__.py (_MAX_SCAN_BYTES there) —
# kept independent so this module stays stdlib-only and importable in isolation.
# If you change one, change both.
_MAX_SCAN_BYTES = 256 * 1024

# Obvious non-secrets — example keys, placeholders, redactions. Checked against
# the matched text so AWS's documented ``AKIAIOSFODNN7EXAMPLE`` and friends, or
# ``api_key = "your-key-here"``, don't generate false warnings.
# Two exclusion sets:
#   _EXAMPLE_RE   — unambiguous "this is documentation, not a real key" words.
#     Safe to apply even to fixed-prefix tokens (AKIA…/ghp_…), because a real
#     random key won't contain the literal word "example"/"dummy"/etc.
#   _PLACEHOLDER_RE — broader, includes structural fillers (your-, xxxx, 0000,
#     <...>). Applied ONLY to assignment-style/entropy values, never to a
#     fixed-prefix token — otherwise a real key that merely *contains* "xxxx"
#     or "0000" as a substring would be silently dropped (a fail-open miss in
#     a security tool). See scan_secrets().
_EXAMPLE_RE = re.compile(
    r"(?i)(example|redacted|placeholder|dummy|sample|changeme|fake|"
    r"test[_-]?(?:key|token|secret))"
)
_PLACEHOLDER_RE = re.compile(
    r"(?i)(example|redacted|placeholder|dummy|sample|changeme|your[_-]?|"
    r"x{4,}|\.\.\.|<[a-z0-9_ .-]+>|fake|test[_-]?(?:key|token|secret)|0{8,})"
)

_SECRET_REMINDER = (
    "⚠️ Security Warning: a hardcoded credential ({kind}) appears in "
    "this content. Never commit live secrets to source. Move it to an "
    "environment variable or a secrets manager, and rotate the credential if it "
    "was ever real. If this is a placeholder/example, document that inline."
)

_ENTROPY_REMINDER = (
    "⚠️ Security Warning: a high-entropy value is assigned to a "
    "secret-named variable — this looks like a hardcoded credential. Move it to "
    "an environment variable or secrets manager and rotate it if real. If it is "
    "not a secret, rename the variable or document why it is safe."
)

# (ruleName, human-readable kind, compiled regex). Most-specific first.
_SECRET_RULES: List[Tuple[str, str, "re.Pattern[str]"]] = [
    ("private_key_pem", "PEM private key",
     re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |PGP |ENCRYPTED )?PRIVATE KEY-----")),
    ("aws_access_key_id", "AWS access key id",
     re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("aws_secret_access_key", "AWS secret access key",
     re.compile(r"(?i)aws_secret_access_key\s*[=:]\s*[\"'][A-Za-z0-9/+]{40}[\"']")),
    ("github_token", "GitHub token",
     re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b")),
    ("github_pat_finegrained", "GitHub fine-grained PAT",
     re.compile(r"\bgithub_pat_[A-Za-z0-9_]{22,}\b")),
    ("slack_token", "Slack token",
     re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),
    ("slack_webhook", "Slack webhook URL",
     re.compile(r"https://hooks\.slack\.com/services/T[A-Za-z0-9_/]+")),
    ("google_api_key", "Google API key",
     re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b")),
    ("stripe_secret_key", "Stripe secret key",
     re.compile(r"\b(?:sk|rk)_live_[0-9a-zA-Z]{24,}\b")),  # live keys only; sk_test_ is low-risk by design
    ("npm_token", "npm token",
     re.compile(r"\bnpm_[A-Za-z0-9]{36}\b")),
    ("jwt_token", "JSON Web Token",
     re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b")),
    ("generic_secret_assignment", "hardcoded API key / token",
     re.compile(
         r"(?i)\b(?:api[_-]?key|client[_-]?secret|access[_-]?token|auth[_-]?token|"
         r"secret[_-]?key)\b\s*[=:]\s*[\"'][A-Za-z0-9_\-]{16,}[\"']"
     )),
]

# Entropy layer: a high-entropy value assigned to a secret-named key.
_SECRET_ASSIGN_RE = re.compile(
    r"(?i)\b([A-Za-z0-9_]*(?:secret|token|passwd|password|api[_-]?key|"
    r"access[_-]?key|client[_-]?secret|private[_-]?key|credential)[A-Za-z0-9_]*)"
    r"\s*[=:]\s*[\"']([^\"'\s]{20,})[\"']"
)
_ENTROPY_THRESHOLD = 4.0  # bits/char; random base64 ~5-6, English prose ~4.0-4.2


def shannon_entropy(s: str) -> float:
    """Shannon entropy in bits/char of *s* (0.0 for empty)."""
    if not s:
        return 0.0
    counts: Dict[str, int] = {}
    for ch in s:
        counts[ch] = counts.get(ch, 0) + 1
    n = len(s)
    return -sum((c / n) * math.log2(c / n) for c in counts.values())


def _is_placeholder(value: str) -> bool:
    return bool(_PLACEHOLDER_RE.search(value))


def _too_big(content: str) -> bool:
    return len(content.encode("utf-8", errors="ignore")) > _MAX_SCAN_BYTES


def scan_secrets(path: str, content: str) -> List[Tuple[str, str]]:
    """Return ``[(ruleName, reminder), ...]`` for credentials found in *content*.

    Each rule fires at most once. Obvious placeholders/example values are
    excluded to keep the false-positive rate low. *path* is accepted for
    symmetry with the regex scanner; secrets are scanned in any file type
    (config/.env files matter most).
    """
    if not content or _too_big(content):
        return []
    hits: List[Tuple[str, str]] = []
    seen: Set[str] = set()
    for rule_name, kind, rx in _SECRET_RULES:
        m = rx.search(content)
        if not m or rule_name in seen:
            continue
        # Fixed-prefix rules are high-precision — only suppress documented
        # EXAMPLE-style tokens. The assignment-style rule's value can legitimately
        # be a structural placeholder ("your-key-here"), so it gets the broad set.
        excl = _PLACEHOLDER_RE if rule_name == "generic_secret_assignment" else _EXAMPLE_RE
        if excl.search(m.group(0)):
            continue
        seen.add(rule_name)
        hits.append((rule_name, _SECRET_REMINDER.format(kind=kind)))
    # Entropy backstop — only when no known-format secret already fired, so a
    # single hardcoded secret never produces two near-duplicate warnings.
    if not hits:
        for m in _SECRET_ASSIGN_RE.finditer(content):
            value = m.group(2)
            if _is_placeholder(value):
                continue
            if shannon_entropy(value) >= _ENTROPY_THRESHOLD:
                hits.append(("high_entropy_secret", _ENTROPY_REMINDER))
                break
    return hits
