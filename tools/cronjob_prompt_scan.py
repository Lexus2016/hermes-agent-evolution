"""Cron prompt threat scanning: the small user-authored prompt gets the strict pattern set;
the assembled prompt (with skill bodies) gets only prose-proof directives."""

import logging
import re

# Single source of truth shared with the install-time scanner (skills_guard): a narrower
# cron-local copy once let obfuscated directives slip past this runtime tripwire.
from tools.threat_patterns import INVISIBLE_CHARS as _CRON_INVISIBLE_CHARS

# Logger parity with the origin module (these functions used to log there).
logger = logging.getLogger("tools.cronjob_tools")

# Strict patterns — user prompt only. A directive-shaped cron prompt has no business
# containing `cat ~/.hermes/.env` or `rm -rf /`; there it is a smoking gun, not prose.
# Two threat surfaces, two scanners: 1. `_scan_cron_prompt()` runs against this at create/update time and as
# a runtime defense-in-depth. 2. Assembled prompt that includes loaded skill content (large markdown bodies,
# often security docs, postmortems, runbooks discussing attack patterns in PROSE). Reusing the strict
# patterns here false-positives every time a skill *describes* a command — see #3968 follow-up: the
# `hermes-agent-dev` skill contains a security postmortem mentioning `cat ~/.hermes/.env`, which tripped
# `read_secrets` and silently killed all PR-scout jobs. Skill bodies are user-curated and scanned at install
# time by `skills_guard.py`. The runtime cron scan only needs to catch the patterns whose phrasing does NOT
# survive normal English prose: classic prompt-injection directives ("ignore previous instructions",
# "disregard your rules"), deception directives, and invisible unicode. `_scan_cron_skill_assembled()` runs
# against the assembled prompt with this tighter pattern set. Both scanners share the invisible-unicode
# check and the GitHub Authorization header exemption.
_CRON_THREAT_PATTERNS = [
    (r'ignore\s+(?:\w+\s+)*(?:previous|all|above|prior)\s+(?:\w+\s+)*instructions', "prompt_injection"),
    (r'do\s+not\s+tell\s+the\s+user', "deception_hide"),
    (r'system\s+prompt\s+override', "sys_prompt_override"),
    (r'disregard\s+(your|all|any)\s+(instructions|rules|guidelines)', "disregard_rules"),
    (r'cat\s+[^\n]*(\.env|credentials|\.netrc|\.pgpass|id_rsa|id_ed25519|id_ecdsa)', "read_secrets"),
    (r'authorized_keys', "ssh_backdoor"), (r'/etc/sudoers|visudo', "sudoers_mod"),
    (r'rm\s+-rf\s+/', "destructive_root_rm"),
]

# Looser set for the assembled prompt: command-shape patterns are dropped because skill
# markdown (postmortems, runbooks) legitimately *describes* those commands and skill bodies
# are vetted at install time — only unambiguous injection directives remain.
_CRON_SKILL_ASSEMBLED_PATTERNS = _CRON_THREAT_PATTERNS[:4]

_CRON_SECRET_VAR_RE = r'\$\{?\w*(?:KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL|API)\w*\}?'
# Obvious leak paths only: secret in the destination URL, in a POST/form body, or in an
# Authorization header to an arbitrary host.
_CRON_EXFIL_COMMAND_PATTERNS = [
    (rf'curl\s+[^\n]*https?://[^\s"\'`]*{_CRON_SECRET_VAR_RE}', "exfil_curl_url"),
    (rf'wget\s+[^\n]*https?://[^\s"\'`]*{_CRON_SECRET_VAR_RE}', "exfil_wget_url"),
    (rf'curl\s+[^\n]*(?:--data(?:-raw|-binary|-urlencode)?|-d|--form|-F)\s+[^\n]*{_CRON_SECRET_VAR_RE}', "exfil_curl_data"),
    (rf'wget\s+[^\n]*--post-(?:data|file)=[^\n]*{_CRON_SECRET_VAR_RE}', "exfil_wget_post"),
    (rf'curl\s+[^\n]*(?:-H|--header)\s+["\']Authorization:\s*(?:Bearer|token)\s+{_CRON_SECRET_VAR_RE}["\']', "exfil_curl_auth_header"),
]

# U+200D (ZWJ) is a required part of many emoji sequences (👨‍👩‍👧, 🏳️‍🌈): block it
# between plain text, allow it inside an emoji grapheme cluster.
_EMOJI_NEIGHBOUR_CP_RANGES = ((0x1F000, 0x1FFFF), (0x2600, 0x27BF), (0x2300, 0x23FF), (0x1F1E6, 0x1F1FF), (0x20E3, 0x20E3))
_VARIATION_SELECTOR_CP = 0xFE0F


def _is_emoji_cp(cp: int) -> bool:
    return any(lo <= cp <= hi for lo, hi in _EMOJI_NEIGHBOUR_CP_RANGES)


def _is_script_using_zwj(cp: int) -> bool:
    """Return True for codepoints from scripts that legitimately use ZWJ."""
    # Arabic (U+0600–U+06FF, U+0750–U+077F, U+08A0–U+08FF, U+FB50–U+FDFF, U+FE70–U+FEFF)
    if 0x0600 <= cp <= 0x06FF:
        return True
    if 0x0750 <= cp <= 0x077F:
        return True
    if 0x08A0 <= cp <= 0x08FF:
        return True
    if 0xFB50 <= cp <= 0xFDFF:
        return True
    if 0xFE70 <= cp <= 0xFEFF:
        return True
    # Devanagari (U+0900–U+097F)
    if 0x0900 <= cp <= 0x097F:
        return True
    # Bengali (U+0980–U+09FF)
    if 0x0980 <= cp <= 0x09FF:
        return True
    # Gurmukhi (U+0A00–U+0A7F)
    if 0x0A00 <= cp <= 0x0A7F:
        return True
    # Gujarati (U+0A80–U+0AFF)
    if 0x0A80 <= cp <= 0x0AFF:
        return True
    # Oriya (U+0B00–U+0B7F)
    if 0x0B00 <= cp <= 0x0B7F:
        return True
    # Tamil (U+0B80–U+0BFF)
    if 0x0B80 <= cp <= 0x0BFF:
        return True
    # Telugu (U+0C00–U+0C7F)
    if 0x0C00 <= cp <= 0x0C7F:
        return True
    # Kannada (U+0C80–U+0CFF)
    if 0x0C80 <= cp <= 0x0CFF:
        return True
    # Malayalam (U+0D00–U+0D7F)
    if 0x0D00 <= cp <= 0x0D7F:
        return True
    # Sinhala (U+0D80–U+0DFF)
    if 0x0D80 <= cp <= 0x0DFF:
        return True
    # Hebrew (U+0590–U+05FF)
    if 0x0590 <= cp <= 0x05FF:
        return True
    # Syriac (U+0700–U+074F)
    if 0x0700 <= cp <= 0x074F:
        return True
    # Thaana (U+0780–U+07BF)
    if 0x0780 <= cp <= 0x07BF:
        return True
    # Myanmar (U+1000–U+109F)
    if 0x1000 <= cp <= 0x109F:
        return True
    # Khmer (U+1780–U+17FF)
    if 0x1780 <= cp <= 0x17FF:
        return True
    # Tibetan (U+0F00–U+0FFF)
    if 0x0F00 <= cp <= 0x0FFF:
        return True
    # Georgian (U+10A0–U+10FF, U+2D00–U+2D2F)
    if 0x10A0 <= cp <= 0x10FF:
        return True
    if 0x2D00 <= cp <= 0x2D2F:
        return True
    # Ethiopic (U+1200–U+137F)
    if 0x1200 <= cp <= 0x137F:
        return True
    # Cherokee (U+13A0–U+13FF)
    if 0x13A0 <= cp <= 0x13FF:
        return True
    # Canadian Aboriginal (U+1400–U+167F)
    if 0x1400 <= cp <= 0x167F:
        return True
    # Mongolian (U+1800–U+18AF)
    if 0x1800 <= cp <= 0x18AF:
        return True
    # Limbu (U+1900–U+194F)
    if 0x1900 <= cp <= 0x194F:
        return True
    # Tai Le (U+1950–U+197F)
    if 0x1950 <= cp <= 0x197F:
        return True
    # New Tai Lue (U+1980–U+19DF)
    if 0x1980 <= cp <= 0x19DF:
        return True
    # Buginese (U+1A00–U+1A1F)
    if 0x1A00 <= cp <= 0x1A1F:
        return True
    # Tai Tham (U+1A20–U+1AAF)
    if 0x1A20 <= cp <= 0x1AAF:
        return True
    # Balinese (U+1B00–U+1B7F)
    if 0x1B00 <= cp <= 0x1B7F:
        return True
    # Sundanese (U+1B80–U+1BBF)
    if 0x1B80 <= cp <= 0x1BBF:
        return True
    # Batak (U+1BC0–U+1BFF)
    if 0x1BC0 <= cp <= 0x1BFF:
        return True
    # Lepcha (U+1C00–U+1C4F)
    if 0x1C00 <= cp <= 0x1C4F:
        return True
    # Ol Chiki (U+1C50–U+1C7F)
    if 0x1C50 <= cp <= 0x1C7F:
        return True
    # Cyrillic (U+0400–U+04FF, U+0500–U+052F, U+2DE0–U+2DFF, U+A640–U+A69F)
    if 0x0400 <= cp <= 0x04FF:
        return True
    if 0x0500 <= cp <= 0x052F:
        return True
    if 0x2DE0 <= cp <= 0x2DFF:
        return True
    if 0xA640 <= cp <= 0xA69F:
        return True
    return False


def _zwj_has_emoji_neighbour(text: str, idx: int) -> bool:
    """True when the ZWJ at text[idx] sits between emoji codepoints (skipping VS16)."""
    left = idx - 1
    while left >= 0 and ord(text[left]) == _VARIATION_SELECTOR_CP:
        left -= 1
    right = idx + 1
    while right < len(text) and ord(text[right]) == _VARIATION_SELECTOR_CP:
        right += 1
    if left < 0 or right >= len(text):
        return False
    return all(
        any(lo <= ord(text[pos]) <= hi for lo, hi in _EMOJI_NEIGHBOUR_CP_RANGES) for pos in (left, right)
    )


def _zwj_has_script_neighbour(text: str, idx: int) -> bool:
    """Return True when the ZWJ at text[idx] sits between letters from
    scripts that legitimately use ZWJ for joining/ligature control."""
    left = idx - 1
    while left >= 0 and ord(text[left]) == _VARIATION_SELECTOR_CP:
        left -= 1
    right = idx + 1
    while right < len(text) and ord(text[right]) == _VARIATION_SELECTOR_CP:
        right += 1
    if left < 0 or right >= len(text):
        return False
    return _is_script_using_zwj(ord(text[left])) and _is_script_using_zwj(
        ord(text[right])
    )


def _strip_cron_safe_constructs(prompt: str) -> str:
    """Scrub the bundled GitHub skill's `Authorization: token $GITHUB_TOKEN` + api.github.com
    curl so it doesn't trip the auth-header exfil rule.

    re.sub scrubs EVERY occurrence. The trailing ``[^\\s;&|$`]*`` consumes only the URL path —
    never separators or subshell openers — so a payload smuggled onto the same line still gets
    scanned. Host must be exactly api.github.com followed by ``/``, whitespace, quote, or end:
    lookalike authorities (api.github.com.evil.com, api.github.com@evil.com) fall through.
    """
    return re.sub(
        rf'curl\s+[^\n;&|$`]*(?:-H|--header)\s+["\']Authorization:\s*token\s+{_CRON_SECRET_VAR_RE}["\']'
        r'\s+["\']?https://api\.github\.com(?::\d+)?(?:/|\s|$|["\'])[^\s;&|$`]*',
        'curl https://api.github.com/user',
        prompt,
        flags=re.IGNORECASE,
    )


def _strip_invisible_unicode(prompt: str) -> tuple[str, list[str]]:
    """Strip invisible-unicode chars, keeping ZWJ inside legitimate emoji.

    Returns ``(cleaned, sorted U+XXXX labels removed)``. The skills-attached path sanitizes
    (a stray zero-width space in vetted skill content must not permanently kill the job).
    """
    if not prompt:
        return prompt, []
    removed: set[str] = set()
    cleaned: list[str] = []
    for idx, ch in enumerate(prompt):
        if ch in _CRON_INVISIBLE_CHARS and not (
            ch == '\u200d' and (_zwj_has_emoji_neighbour(prompt, idx) or _zwj_has_script_neighbour(prompt, idx))
        ):
            removed.add(f"U+{ord(ch):04X}")
            continue
        cleaned.append(ch)
    return ''.join(cleaned), sorted(removed)


def _first_pattern_error(text: str, *pattern_sets) -> str:
    for patterns in pattern_sets:
        for pattern, pid in patterns:
            if re.search(pattern, text, re.IGNORECASE):
                return (
                    f"Blocked: prompt matches threat pattern '{pid}'. Cron prompts must not "
                    "contain injection or exfiltration payloads."
                )
    return ""


def _scan_cron_prompt(prompt: str) -> str:
    """Strict scan of the USER-SUPPLIED prompt (create/update + runtime defense-in-depth).
    Returns an error string when blocked, else "". Invisible unicode is reported first, in
    ``_CRON_INVISIBLE_CHARS`` order (emoji ZWJ allowed)."""
    prompt_to_scan = _strip_cron_safe_constructs(prompt)
    removed = set(_strip_invisible_unicode(prompt_to_scan)[1])
    for char in _CRON_INVISIBLE_CHARS:
        if f"U+{ord(char):04X}" in removed:
            return f"Blocked: prompt contains invisible unicode U+{ord(char):04X} (possible injection)."
    return _first_pattern_error(prompt_to_scan, _CRON_THREAT_PATTERNS, _CRON_EXFIL_COMMAND_PATTERNS)


def _scan_cron_skill_assembled(assembled: str) -> tuple[str, str]:
    """Loose scan of the ASSEMBLED prompt (skill content included). Invisible unicode is
    SANITIZED (stripped + logged), not blocked — the hard block stays on raw user prompts,
    the actual injection surface. Returns ``(cleaned_prompt, error)``, error "" when passed."""
    cleaned, removed = _strip_invisible_unicode(assembled)
    if removed:
        logger.warning(
            "Cron skill-assembled prompt: stripped %d invisible-unicode "
            "char(s) (%s) from vetted skill content",
            len(removed), ", ".join(removed),
        )
    return cleaned, _first_pattern_error(_strip_cron_safe_constructs(cleaned), _CRON_SKILL_ASSEMBLED_PATTERNS)
