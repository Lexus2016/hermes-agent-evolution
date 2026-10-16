"""Pure response/error diagnostics shared by turn recovery branches."""
from __future__ import annotations
import logging
import math
import re
from typing import Any, Optional

def _image_error_max_dimension(error: Exception) -> Optional[int]:
    """Extract a provider-reported image dimension ceiling, if present."""
    parts = []
    for value in (error, getattr(error, "message", None), getattr(error, "body", None)):
        if value:
            try:
                parts.append(str(value))
            except Exception:
                pass
    text = " ".join(parts).lower()
    # OpenAI Codex Responses reports a tile-patch budget (ceil(w/32)×ceil(h/32))
    # instead of a pixel ceiling. A square image is the worst case for the budget,
    # so a per-side cap of isqrt(limit)*32 px keeps isqrt(limit)² ≤ limit — for the
    # 30000-patch ceiling that is 5536 px. Without this the caller falls back to
    # 8000 px and a 6000 px image that already exceeds the budget is skipped (#106337).
    if "patches after processing" in text:
        match = re.search(r"exceeding the limit of\s*(\d{2,7})", text)
        if not match:
            return None
        max_dimension = math.isqrt(int(match.group(1))) * 32
        return max_dimension if 512 <= max_dimension <= 8000 else None
    if "image" not in text or "dimension" not in text or "max allowed size" not in text:
        return None
    match = re.search(r"max allowed size(?:\s+for [^:]+)?:\s*(\d{3,5})\s*pixels?", text)
    if not match:
        return None
    try:
        max_dimension = int(match.group(1))
    except ValueError:
        return None
    return max_dimension if 512 <= max_dimension <= 8000 else None

def _codex_soft_failure_error(response: Any) -> dict[str, Any]:
    """``response.error`` of a Codex ``failed``/``cancelled`` Response as ``{"code", "message"}``
    (the SDK types it as ``ResponseError``; the raw-SSE assembler keeps the dict); ``{}`` when absent."""
    error_obj = getattr(response, "error", None)
    if not error_obj:
        return {}
    if isinstance(error_obj, dict):
        fields = error_obj
    elif hasattr(error_obj, "code") or hasattr(error_obj, "message"):
        fields = {"code": getattr(error_obj, "code", None), "message": getattr(error_obj, "message", None)}
    else:
        fields = {"message": str(error_obj)}
    return {k: v for k, v in fields.items() if isinstance(v, str) and v.strip()}

def describe_invalid_response(agent: Any, response: Any, api_duration: float) -> tuple[str, str, str]:
    """Diagnostics for an empty/malformed response: ``(error_msg, provider_name,
    failure_hint)``. The hint is derived from the provider error code (524/504/429/
    5xx) and the response time, instead of always assuming rate limiting."""
    error_msg = "Unknown"
    provider_name = "Unknown"
    _has_error = bool(response and hasattr(response, 'error') and response.error)
    if _has_error:
        # A typed ``ResponseError`` stringifies as its repr; show the provider's message.
        error_msg = _codex_soft_failure_error(response).get("message") or str(response.error)
        if hasattr(response.error, 'metadata') and response.error.metadata:
            provider_name = response.error.metadata.get('provider_name', 'Unknown')
    elif response and hasattr(response, 'message') and response.message:
        error_msg = str(response.message)

    # OpenRouter often returns the actual model used.
    if provider_name == "Unknown" and response and hasattr(response, 'model') and response.model:
        provider_name = f"model={response.model}"

    if provider_name == "Unknown" and response:
        resp_attrs = {k: str(v)[:100] for k, v in vars(response).items() if not k.startswith('_')}
        if agent.verbose_logging:
            logging.debug(f"Response attributes for invalid response: {resp_attrs}")

    _resp_error_code = None
    if _has_error:
        _code_raw = getattr(response.error, 'code', None)
        if _code_raw is None and isinstance(response.error, dict):
            _code_raw = response.error.get('code')
        if _code_raw is not None:
            try:
                _resp_error_code = int(_code_raw)
            except (TypeError, ValueError):
                pass

    return error_msg, provider_name, _failure_hint_for(_resp_error_code, api_duration)

def _failure_hint_for(code: Optional[int], api_duration: float) -> str:
    """Human-readable hint from the provider error code and response time."""
    if code == 524:
        return f"upstream provider timed out (Cloudflare 524, {api_duration:.0f}s)"
    if code == 504:
        return f"upstream gateway timeout (504, {api_duration:.0f}s)"
    if code == 429:
        return "rate limited by upstream provider (429)"
    if code in {500, 502}:
        return f"upstream server error ({code}, {api_duration:.0f}s)"
    if code in {503, 529}:
        return f"upstream provider overloaded ({code})"
    if code is not None:
        return f"upstream error (code {code}, {api_duration:.0f}s)"
    if api_duration < 10:
        return f"fast response ({api_duration:.1f}s) — likely rate limited"
    if api_duration > 60:
        return f"slow response ({api_duration:.0f}s) — likely upstream timeout"
    return f"response time {api_duration:.1f}s"
