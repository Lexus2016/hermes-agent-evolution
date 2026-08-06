"""Neutralize prompt-injection patterns in tool-returned data (#1715).

Tool output is untrusted: it may carry injected instructions from web pages,
files, or remote APIs. Before a tool result re-enters model context we detect
common injection markers and wrap the offending segment in a provenance fence
so the model reads it as *data*, not as instructions. Conservative by design:
we never delete content, only bracket segments that look like directive
attempts. Fail-open: on any error the original text passes through unchanged.
"""

import re

# Directive-style markers that frequently appear in injected tool output.
_IGNORE_PREFIX = re.compile(
    r"(?im)^\s*(?:ignore|disregard|forget|override)\s+(?:(?:all|any|above|previous|prior|earlier|the)\s+)*(?:instructions?|prompts?|rules?|context)\b",
)
_ROLE_TAG = re.compile(r"(?im)^\s*(system|assistant|human|user):\s*")
_PERSONA_HIJACK = re.compile(
    r"(?im)^\s*(you\s+are|act\s+as|from\s+now\s+on|pretend\s+to\s+be)\b"
)
_DO_NOW = re.compile(r"(?im)^\s*(now|next|then|important)\s*[,:]\s*")
_FENCE_OPEN = "[untrusted-tool-data:"
_FENCE_CLOSE = "]"


def _neutralize(text: str) -> str:
    """Fence an injected-looking segment so it is read as data, not commands."""
    lines = text.split("\n")
    out: list[str] = []
    fenced = False
    for line in lines:
        if not fenced and _looks_injected(line):
            out.append(_FENCE_OPEN)
            fenced = True
        elif fenced and not line.strip():
            out.append(_FENCE_CLOSE)
            fenced = False
        out.append(line)
    if fenced:
        out.append(_FENCE_CLOSE)
    return "\n".join(out)


def _looks_injected(line: str) -> bool:
    stripped = line.strip()
    if not stripped:
        return False
    return bool(
        _IGNORE_PREFIX.search(stripped)
        or _ROLE_TAG.match(stripped)
        or _PERSONA_HIJACK.match(stripped)
        or _DO_NOW.match(stripped)
    )


def sanitize_tool_result(result: object) -> object:
    """Return ``result`` with injected directive segments fenced off.

    The fencing markers delimit data that should not be obeyed as instructions.
    Non-string inputs and errors pass through unchanged (fail-open).
    """
    if not isinstance(result, str) or not result:
        return result
    try:
        return _neutralize(result)
    except Exception:
        return result
