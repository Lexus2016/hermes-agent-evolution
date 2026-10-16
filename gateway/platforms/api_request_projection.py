"""OpenAI request/content normalization and transcript projection for the API adapter."""
from __future__ import annotations
from contextlib import suppress
from typing import Any, Optional

RESPONSES_AUTO_TRUNCATION_HISTORY_LIMIT = 100

MAX_NORMALIZED_TEXT_LENGTH = 65_536  # 64 KB cap for normalized content parts

MAX_CONTENT_LIST_SIZE = 1_000  # Max items when content is an array

_TRUE_REQUEST_BOOL_STRINGS = frozenset({"1", "true", "yes", "on"})

_FALSE_REQUEST_BOOL_STRINGS = frozenset({"0", "false", "no", "off"})

def _coerce_request_bool(value: Any, default: bool = False) -> bool:
    """Normalize boolean-like payload values; only explicit bool-ish scalars count (some
    frontends send ``"false"`` for ``stream``, which is truthy), else ``default``."""
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in _TRUE_REQUEST_BOOL_STRINGS:
            return True
        return False if normalized in _FALSE_REQUEST_BOOL_STRINGS else default
    return bool(value) if isinstance(value, (int, float)) else default

_RUNTIME_AGENT_OVERRIDE_KEYS = (
    "api_key", "base_url", "provider", "api_mode", "command", "args", "credential_pool")

def _clean_request_string(value: Any) -> Optional[str]:
    """Return a stripped request string, or None for absent/non-string values."""
    return (value.strip() or None) if isinstance(value, str) else None

def _apply_runtime_agent_overrides(
    runtime_kwargs: dict[str, Any], overrides: Optional[dict[str, Any]]) -> dict[str, Any]:
    """Merge resolved provider/runtime fields into ``runtime_kwargs`` in place."""
    if not isinstance(overrides, dict):
        return runtime_kwargs
    for key in _RUNTIME_AGENT_OVERRIDE_KEYS:
        value = overrides.get(key)
        if value is None:
            continue
        runtime_kwargs[key] = list(value) if key == "args" and isinstance(value, (list, tuple)) else value
    return runtime_kwargs

def _resolve_request_runtime_agent_kwargs(provider: str, target_model: Optional[str] = None) -> dict[str, Any]:
    """gateway.run._resolve_runtime_agent_kwargs() for an explicit provider/model, so an API
    caller uses the same authenticated provider catalog without mutating config.yaml."""
    from hermes_cli.runtime_provider import resolve_runtime_provider, format_runtime_provider_error, _get_model_config
    try:
        runtime = resolve_runtime_provider(requested=provider, target_model=target_model)
    except Exception as exc:
        raise RuntimeError(format_runtime_provider_error(exc)) from exc

    return {
        **{k: runtime.get(k) for k in ("api_key", "base_url", "provider", "api_mode", "command")},
        "args": list(runtime.get("args") or []),
        "credential_pool": runtime.get("credential_pool")}

def _request_agent_overrides(
    body: Any, *, virtual_model: Optional[str] = None, allow_bare_model: bool = True
) -> dict[str, Any]:
    """Extract per-request model/provider/options for _run_agent.

    The virtual model (``hermes-agent``) means "gateway default". A bare ``model`` without
    ``provider`` is honored only when ``allow_bare_model`` (generic clients hardcode "gpt-4o";
    OpenAI-compatible handlers pass the ``direct_model_requests`` opt-in, Hermes-native
    endpoints always allow it). An explicit ``provider`` is always honored.
    """
    if not isinstance(body, dict):
        return {}
    overrides: dict[str, Any] = {}
    provider = _clean_request_string(body.get("provider"))
    if provider:
        overrides["requested_provider"] = provider
    model = _clean_request_string(body.get("model"))
    if model and model != virtual_model and (provider or allow_bare_model):
        overrides["requested_model"] = model
    model_options = body.get("model_options")
    if isinstance(model_options, dict):
        overrides["model_options"] = dict(model_options)
    return overrides

def _request_relay_metadata(body: Any) -> dict[str, Any]:
    """Extract Relay metadata from an OpenAI request body."""
    if not isinstance(body, dict):
        return {}
    metadata = body.get("metadata")
    if not isinstance(metadata, dict):
        return {}
    return dict(metadata)

def _is_compressed_summary_message(message: Any) -> bool:
    """Recognize every compaction carrier shape via the compressor's own classifier
    (SessionDB drops the in-process marker; a prefix scan misses merge-into-tail carriers)."""
    if not isinstance(message, dict):
        return False
    from agent.context_compressor import is_compaction_summary_message
    return is_compaction_summary_message(message)

def _project_client_message(message: dict[str, Any]) -> dict[str, Any]:
    """Strip compaction scaffolding: standalone handoffs become hidden empty rows (stable
    ids), merged handoffs keep only the real prior-tail content; inherited tool calls dropped."""
    from agent.compaction_display import (
        _COMPACTION_INTERNAL_FIELDS, project_compaction_message_for_display)
    if (message.get("display_kind") == "hidden"
            and (message.get("display_metadata") or {}).get("notification_category") == "diagnostic"):
        # Retain row identity and execution evidence in storage, not in the notification UI.
        return {k: v for k, v in message.items() if k in {
            "id", "session_id", "role", "timestamp", "display_kind", "platform_message_id",
        }} | {"content": ""}
    projected = project_compaction_message_for_display(message)
    if projected is None:
        projected = {k: v for k, v in message.items() if k not in _COMPACTION_INTERNAL_FIELDS}
        projected["content"] = ""
        projected["display_kind"] = "hidden"
    return projected

def _auto_truncate_response_history(
    conversation_history: list[dict[str, Any]],
    *,
    limit: int = RESPONSES_AUTO_TRUNCATION_HISTORY_LIMIT) -> list[dict[str, Any]]:
    """Keep the most recent ``limit`` messages, always preserving compaction summaries
    wherever they sit (the /compress path can leave them after a retained system head)."""
    if limit <= 0 or len(conversation_history) <= limit:
        return conversation_history
    summary_indices = [i for i, m in enumerate(conversation_history) if _is_compressed_summary_message(m)]
    if not summary_indices:
        return conversation_history[-limit:]
    kept_indices = set(summary_indices[:limit])
    remaining = limit - len(kept_indices)
    if remaining > 0:
        summary_index_set = set(summary_indices)
        for index in range(len(conversation_history) - 1, -1, -1):
            if index in summary_index_set:
                continue
            kept_indices.add(index)
            remaining -= 1
            if remaining <= 0:
                break
    return [conversation_history[index] for index in sorted(kept_indices)]

def _cap_text(text: str) -> str:
    return text[:MAX_NORMALIZED_TEXT_LENGTH] if len(text) > MAX_NORMALIZED_TEXT_LENGTH else text

def _cap_list(items: list) -> list:
    return items[:MAX_CONTENT_LIST_SIZE] if len(items) > MAX_CONTENT_LIST_SIZE else items

def _normalize_chat_content(content: Any, *, _max_depth: int = 10, _depth: int = 0) -> str:
    """Flatten OpenAI chat content (string or typed-part array) into one plain string; non-text
    parts are skipped, recursion depth / list size / output length are bounded."""
    if _depth > _max_depth or content is None:
        return ""
    if isinstance(content, str):
        return _cap_text(content)
    if isinstance(content, list):
        parts: list[str] = []
        total_len = 0
        for item in _cap_list(content):
            part = ""
            if isinstance(item, str):
                part = item
            elif isinstance(item, dict):
                if str(item.get("type") or "").strip().lower() in _TEXT_PART_TYPES:
                    text = item.get("text", "")
                    if text:
                        with suppress(Exception):
                            part = str(text)
            elif isinstance(item, list):
                part = _normalize_chat_content(item, _max_depth=_max_depth, _depth=_depth + 1)
            if part:
                part = _cap_text(part)
                parts.append(part)
                total_len += len(part)
            if total_len >= MAX_NORMALIZED_TEXT_LENGTH:
                break
        return _cap_text("\n".join(parts))
    try:
        return _cap_text(str(content))
    except Exception:
        return ""

_TEXT_PART_TYPES = frozenset({"text", "input_text", "output_text"})

_IMAGE_PART_TYPES = frozenset({"image_url", "input_image"})

_FILE_PART_TYPES = frozenset({"file", "input_file"})

def _normalize_image_part(part: dict[str, Any]) -> dict[str, Any]:
    """Validate one image part (Responses top-level ``image_url`` string or Chat Completions
    ``{"url", "detail"}`` dict) into the canonical vision shape; raises ValueError."""
    detail = part.get("detail")
    image_ref = part.get("image_url")
    if isinstance(image_ref, dict):
        url_value = image_ref.get("url")
        detail = image_ref.get("detail", detail)
    else:
        url_value = image_ref
    if not isinstance(url_value, str) or not url_value.strip():
        raise ValueError("invalid_image_url:Image parts must include a non-empty image URL.")
    url_value = url_value.strip()
    lowered = url_value.lower()
    if lowered.startswith("data:"):
        if not lowered.startswith("data:image/") or "," not in url_value:
            raise ValueError(
                "unsupported_content_type:Only image data URLs are supported. "
                "Non-image data payloads are not supported.")
    elif not (lowered.startswith("http://") or lowered.startswith("https://")):
        raise ValueError(
            "invalid_image_url:Image inputs must use http(s) URLs or data:image/... URLs.")
    image_part: dict[str, Any] = {"type": "image_url", "image_url": {"url": url_value}}
    if detail is not None:
        if not isinstance(detail, str) or not detail.strip():
            raise ValueError("invalid_content_part:Image detail must be a non-empty string when provided.")
        image_part["image_url"]["detail"] = detail.strip()
    return image_part

def _normalize_multimodal_content(content: Any) -> Any:
    """Validate multimodal content: a plain string when text-only, else canonical ``text`` /
    ``image_url`` parts (native OpenAI vision shape; Anthropic conversion happens downstream).

    Raises ``ValueError("<code>:<message>")`` with codes ``unsupported_content_type`` (file
    parts, non-image data URLs, unknown types), ``invalid_image_url``, ``invalid_content_part``.
    """
    if content is None:
        return ""
    if isinstance(content, str):
        return _cap_text(content)
    if not isinstance(content, list):
        return _normalize_chat_content(content)
    normalized_parts: list[dict[str, Any]] = []
    for part in _cap_list(content):
        if isinstance(part, str):
            if part:
                normalized_parts.append({"type": "text", "text": _cap_text(part)})
            continue
        if not isinstance(part, dict):
            continue  # unknown scalars are ignored for forward compatibility (e.g. ``refusal``)
        raw_type = part.get("type")
        part_type = str(raw_type or "").strip().lower()
        if part_type in _TEXT_PART_TYPES:
            text = part.get("text")
            if text is not None and str(text):
                normalized_parts.append({"type": "text", "text": _cap_text(str(text))})
        elif part_type in _IMAGE_PART_TYPES:
            normalized_parts.append(_normalize_image_part(part))
        elif part_type in _FILE_PART_TYPES:
            raise ValueError(
                "unsupported_content_type:Inline image inputs are supported, "
                "but uploaded files and document inputs are not supported on this endpoint.")
        else:
            raise ValueError(
                f"unsupported_content_type:Unsupported content part type {raw_type!r}. "
                "Only text and image_url/input_image parts are supported.")
    if not normalized_parts:
        return ""
    # Text-only collapses to a plain string so trajectory logging and prompt caching see
    # the native shape.
    if all(p.get("type") == "text" for p in normalized_parts):
        return "\n".join(p["text"] for p in normalized_parts if p.get("text"))
    return normalized_parts

def _content_has_visible_payload(content: Any) -> bool:
    """True when content has any text or image attachment.  Used to reject empty turns."""
    if isinstance(content, str):
        return bool(content.strip())
    if isinstance(content, list):
        for part in content:
            if isinstance(part, dict):
                ptype = str(part.get("type") or "").strip().lower()
                if ptype in _IMAGE_PART_TYPES or (
                        ptype in _TEXT_PART_TYPES and str(part.get("text") or "").strip()):
                    return True
    return False

def _request_turn_author(body: dict[str, Any]) -> Optional[dict[str, Any]]:
    """Normalized body ``author``, None when absent or null, ValueError when not an object. It only labels memory."""
    raw = body.get("author")
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError("author must be an object")
    from agent.turn_author import parse_turn_author
    return parse_turn_author(raw)

_USAGE_TOKEN_KEYS = ("input_tokens", "output_tokens", "total_tokens")

def _chat_usage_payload(usage: dict[str, Any]) -> dict[str, int]:
    """OpenAI Chat Completions ``usage`` block (prompt/completion/total) from the agent's usage."""
    values = (usage.get(key, 0) for key in _USAGE_TOKEN_KEYS)
    return dict(zip(("prompt_tokens", "completion_tokens", "total_tokens"), values))

def _responses_usage_payload(usage: dict[str, Any]) -> dict[str, int]:
    """OpenAI Responses ``usage`` block from the agent's usage dict."""
    return {key: usage.get(key, 0) for key in _USAGE_TOKEN_KEYS}
