"""EchoLeak + agentjacking output defenses (#1717).

Tool output and error messages can carry (a) markdown image links that trigger
remote fetching (EchoLeak exfiltration vector) and (b) injected instructions
embedded in crash reports / debug output (agentjacking vector). This module
neutralizes both before they enter model context. Fail-open: any error returns
the original text unchanged.
"""

import re

_IMAGE_LINK = re.compile(r"!\[.*?\]\((https?://[^)]+)\)")
_DATA_IMAGE = re.compile(r"!\[.*?\]\(data:[^)]*\)")
_DEBUG_FENCE = re.compile(
    r"(?m)^(Traceback \(most recent call last\):|Error:|Exception|DEBUG:)"
)
_KEY_REDACT = re.compile(
    r"(?i)(api[_-]?key|token|secret|password|authorization)\s*[:=]\s*\S+"
)
_FENCE_OPEN = "[untrusted-debug:"
_FENCE_CLOSE = "]"


def neutralize_images(text: str) -> str:
    """Replace remote-fetch image links with a safe placeholder."""
    text = _DATA_IMAGE.sub("[blocked-data-image]", text)
    return _IMAGE_LINK.sub("[blocked-image-link]", text)


def fence_debug_output(text: str) -> str:
    """Fence crash-report / debug lines so they are read as data, not commands."""
    lines = text.split("\n")
    out: list[str] = []
    fenced = False
    for line in lines:
        if not fenced and _DEBUG_FENCE.match(line):
            out.append(_FENCE_OPEN)
            fenced = True
        elif fenced and not line.strip():
            out.append(_FENCE_CLOSE)
            fenced = False
        if fenced:
            line = _KEY_REDACT.sub(r"\1=[redacted]", line)
        out.append(line)
    if fenced:
        out.append(_FENCE_CLOSE)
    return "\n".join(out)


def sanitize_output(value: object) -> object:
    """Apply both image neutralization and debug fencing to a tool result."""
    if not isinstance(value, str) or not value:
        return value
    try:
        return fence_debug_output(neutralize_images(value))
    except Exception:
        return value


def sanitize_error_for_context(error_text: str) -> str:
    """Redact secrets from error strings before they enter context."""
    if not isinstance(error_text, str) or not error_text:
        return error_text
    try:
        return _KEY_REDACT.sub(r"\1=[redacted]", error_text)
    except Exception:
        return error_text
