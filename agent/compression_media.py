"""Copy-on-write media eviction and authored-text views used during compaction."""
from __future__ import annotations
import re
from typing import Any, Optional
from agent.image_eviction_policy import outbound_image_retire_count
from agent.turn_context import drop_stale_api_content

_MAX_KEEP_TOOL_IMAGES = 3

_INFLIGHT_TASK_REPLAY_HEADER = (
    "[STILL IN PROGRESS — this is the active request, restated after the "
    "compaction boundary because it was not finished yet. Continue it; do not "
    "start over.]"
)

def _part_text(item: Any) -> Optional[str]:
    """Text of a content part: the string itself, a dict's ``text``, else None."""
    return item if isinstance(item, str) else item.get("text") if isinstance(item, dict) else None

def _with_part_text(item: Any, text: str) -> Any:
    """Copy of a content part carrying ``text`` (string parts become the text itself)."""
    return {**item, "text": text} if isinstance(item, dict) else text

def _content_text_for_contains(content: Any) -> str:
    """Return a best-effort text view of message content (for substring checks only)."""
    if isinstance(content, list):
        return "\n".join(t for t in map(_part_text, content) if isinstance(t, str) and t)
    return "" if content is None else content if isinstance(content, str) else str(content)

_GATEWAY_REPLY_POINTER_RE = re.compile(
    r'\A(?:\[Triggering message id: [^\n]*\]\n\n)?'
    r'\[Replying to(?: your previous message)?: ".*?"\]\n\n',
    re.DOTALL,
)

def _restated_request_text(content: Any) -> str:
    """A user turn's text, stripped, after the last in-flight replay header if present.

    A request restated by an earlier compaction carries the replay header (and possibly the
    old summary) before it; the text after the last header is the request itself, so a task
    that survives several cycles never stacks headers or drags an old summary along.
    """
    text = _content_text_for_contains(content).strip()
    if _INFLIGHT_TASK_REPLAY_HEADER in text:
        text = text.rsplit(_INFLIGHT_TASK_REPLAY_HEADER, 1)[1].strip()
    return text

def _authored_request_text(content: Any) -> str:
    """Text the user wrote in a turn, without the gateway's reply-to pointer.

    The pointer quotes another message verbatim; it disambiguates which message is being
    answered but is not part of the request, so it must not count toward request size.
    """
    return _GATEWAY_REPLY_POINTER_RE.sub("", _restated_request_text(content), count=1).strip()

def _is_text_only_content(content: Any) -> bool:
    """Whether the active request can be restated without losing a content part."""
    if isinstance(content, str):
        return True
    return isinstance(content, list) and all(
        isinstance(part, str)
        or (
            isinstance(part, dict)
            and part.get("type") in {"text", "input_text"}
            and isinstance(part.get("text"), str)
        )
        for part in content
    )

def _append_text_to_content(content: Any, text: str, *, prepend: bool = False) -> Any:
    """Append or prepend plain text to message content (string or multimodal list)."""
    if content is None:
        return text
    if isinstance(content, list):
        text_block = {"type": "text", "text": text}
        return [text_block, *content] if prepend else [*content, text_block]
    rendered = content if isinstance(content, str) else str(content)
    return text + rendered if prepend else rendered + text

def _replace_image_parts(parts: Any, placeholder: str) -> Optional[list[Any]]:
    """New parts list with every image part replaced by a text placeholder; None if no images."""
    if not isinstance(parts, list) or not any(_is_image_part(p) for p in parts):
        return None
    return [{"type": "text", "text": placeholder} if _is_image_part(p) else p for p in parts]

def _tool_result_parts(content: Any) -> Any:
    """Part list of a tool-result body, unwrapping the ``_multimodal`` envelope."""
    return content.get("content") if isinstance(content, dict) and content.get("_multimodal") else content

def _tool_content_has_images(content: Any) -> bool:
    """True when a tool-result body (part list or ``_multimodal`` envelope) carries images."""
    return _content_has_images(_tool_result_parts(content))

def _strip_images_from_tool_msg(msg: dict[str, Any]) -> Optional[dict[str, Any]]:
    """Copy of a tool message with image payloads replaced (stale ``api_content`` dropped); ``None`` if nothing to strip."""
    content = msg.get("content")
    if isinstance(content, dict) and content.get("_multimodal"):
        summary = content.get("text_summary") or "[screenshot removed to save context]"
        return _rewritten(msg, f"[screenshot removed] {str(summary)[:200]}")
    stripped = _replace_image_parts(content, "[screenshot removed to save context]")
    return None if stripped is None else _rewritten(msg, stripped)

def _rewritten(msg: dict[str, Any], content: Any) -> dict[str, Any]:
    """Copy of ``msg`` carrying ``content``; drops the stale ``api_content`` sidecar so replay can't resend it."""
    new_msg = {**msg, "content": content}
    drop_stale_api_content(new_msg)
    return new_msg

def _retire_stale_tool_result_images(
    result: list[dict[str, Any]], keep_newest: int = _MAX_KEEP_TOOL_IMAGES, spared: range = range(0),
) -> int:
    """Replace image payloads on older tool results with text placeholders.
    Keeps the newest ``keep_newest`` image-bearing tool messages and any spared pending round;
    spared images still count toward the newest window. User uploads are untouched. Mutates
    ``result`` in place; returns the number of messages rewritten. Compaction only: it commits the
    rewrite into the canonical transcript once. The send path uses
    :func:`evict_stale_outbound_tool_images` (a per-request keep-newest window rewrites the cached
    prefix on every new image, #113517)."""
    seen = pruned = 0
    for i in range(len(result) - 1, -1, -1):
        msg = result[i]
        if not isinstance(msg, dict) or msg.get("role") != "tool" or not _tool_content_has_images(msg.get("content")):
            continue
        seen += 1
        if seen <= max(keep_newest, 0) or i in spared:
            continue
        new_msg = _strip_images_from_tool_msg(msg)
        if new_msg is not None:
            result[i] = new_msg
            pruned += 1
    return pruned

def _image_payload(msg: dict[str, Any]) -> tuple[int, int]:
    """``(blocks, bytes)`` of image payload in a message.

    The provider counts BLOCKS: one ``tool_result`` carrying three screenshots is three against
    the per-request limit. Bytes are the data-URL / base64 length — the payload is ASCII and the
    JSON framing around it is noise against a 24 MB budget, so no per-request re-serialization.
    """
    parts = _tool_result_parts(msg.get("content"))
    if not isinstance(parts, list):
        return 0, 0
    blocks = payload = 0
    for p in parts:
        if not _is_image_part(p):
            continue
        blocks += 1
        image_url = p.get("image_url")
        source = p.get("source")
        data = (
            (image_url.get("url") if isinstance(image_url, dict) else image_url)
            or (source.get("data") if isinstance(source, dict) else None)
            or ""
        )
        payload += len(data) if isinstance(data, str) else 0
    return blocks, payload

def evict_stale_outbound_tool_images(api_messages: list[dict[str, Any]]) -> int:
    """Drop stale screenshot/vision payloads from the per-call API copy.

    Compression's keep-newest pass only runs when prune/compress fires, and the Anthropic
    adapter's screenshot eviction only sees nested ``tool_result`` blocks. OpenAI-style
    ``image_url`` tool results otherwise ride every subsequent request until a 413 forces
    the reactive strip (#89286). Call this on the cloned ``api_messages`` list after
    sanitization (#89296). Do not pass persisted history — the rewrite is send-path only.

    Eviction is driven by the provider limit, counted in image BLOCKS, with user uploads
    reserved against the ceiling but never rewritten — policy and rationale in
    :mod:`agent.image_eviction_policy`. Returns the number of messages rewritten.
    """
    carriers: list[tuple[int, tuple[int, int]]] = []
    reserved_blocks = reserved_bytes = 0
    for i in range(len(api_messages) - 1, -1, -1):
        msg = api_messages[i]
        if not isinstance(msg, dict):
            continue
        blocks, size = _image_payload(msg)
        if not blocks:
            continue
        if msg.get("role") == "tool":
            carriers.append((i, (blocks, size)))
        else:
            reserved_blocks += blocks
            reserved_bytes += size
    retire = outbound_image_retire_count(
        [blocks for _, (blocks, _) in carriers],
        reserved_blocks,
        carrier_bytes_newest_first=[size for _, (_, size) in carriers],
        reserved_bytes=reserved_bytes,
    )
    pruned = 0
    for i, _ in carriers[len(carriers) - retire:]:
        new_msg = _strip_images_from_tool_msg(api_messages[i])
        if new_msg is not None:
            api_messages[i] = new_msg
            pruned += 1
    return pruned

_IMAGE_PART_TYPES = frozenset({"image_url", "input_image", "image"})

def _is_image_part(part: Any) -> bool:
    """True if ``part`` is an image block (``image_url``, ``input_image``, or ``image``)."""
    return isinstance(part, dict) and part.get("type") in _IMAGE_PART_TYPES

def _content_has_images(content: Any) -> bool:
    """True if a message's ``content`` is a multimodal list with image parts."""
    return isinstance(content, list) and any(_is_image_part(p) for p in content)

def _strip_images_from_content(content: Any) -> Any:
    """``content`` with image parts replaced by placeholders; unchanged (same object) when none."""
    stripped = _replace_image_parts(content, "[Attached image — stripped after compression]")
    return content if stripped is None else stripped

def _strip_historical_media(messages: list[dict[str, Any]], spared: range = range(0)) -> list[dict[str, Any]]:
    """Replace image parts in older messages with placeholder text.
    Rule 1: strip everything before the newest image-bearing user message. Rule 1b: the opening
    attachment ages out once a newer tool image exists. Rule 2: keep only the newest tool-result image,
    except tool results in a spared pending round.
    Unchanged list when nothing applies; input never mutated."""
    if not messages:
        return messages

    def _newest(role: str, has_images) -> int:
        hits = (i for i, m in enumerate(messages) if isinstance(m, dict) and m.get("role") == role)
        return max((i for i in hits if has_images(messages[i].get("content"))), default=-1)

    # Anchor on image-bearing user messages (not all) so a text follow-up still strips the old image.
    anchor = _newest("user", _content_has_images)
    # Tool-result images age on their own timeline: keep only the newest one, wherever it sits.
    # Envelope-aware matcher so the native {_multimodal: True} dict shape anchors too.
    tool_anchor = _newest("tool", _tool_content_has_images)

    if anchor <= 0 and tool_anchor < 0:
        # Nothing to strip under any rule.
        return messages

    def _is_stale(index: int, message: dict[str, Any]) -> bool:
        if index in spared:
            return False
        # Rule 1: everything before the newest image-bearing user message. Rule 1b: the opening
        # attachment ages out once a newer tool image exists (the text placeholder keeps the user row
        # non-empty for the zero-user-turn guard). Rule 2: superseded tool-result image, even in the tail.
        return (
            (0 < anchor and index < anchor)
            # When the ONLY image-bearing user message is the very first one (``anchor == 0``) and newer
            # tool-result images exist, the model has moved on — but the opening base64 blob used to survive
            # every compaction forever, which is half the wedge in #89938 (the reported session opened with
            # a ~200KB poster). When nothing newer exists the opening image IS the newest image and is kept,
            # consistent with keep-newest everywhere else.
            or (anchor == 0 and index == 0 and tool_anchor > 0)
            or (message.get("role") == "tool" and index != tool_anchor)
        )

    def _stripped(i: int, msg: Any) -> Optional[dict[str, Any]]:
        if not isinstance(msg, dict) or not _is_stale(i, msg):
            return None
        content = msg.get("content")
        # Native multimodal envelope: route through the tool-message stripper
        # (collapses to text summary, drops stale api_content sidecar).
        if msg.get("role") == "tool" and isinstance(content, dict) and content.get("_multimodal"):
            return _strip_images_from_tool_msg(msg) if _tool_content_has_images(content) else None
        return _rewritten(msg, _strip_images_from_content(content)) if _content_has_images(content) else None

    result = [(_stripped(i, msg), msg) for i, msg in enumerate(messages)]
    if all(new is None for new, _ in result):
        return messages
    return [msg if new is None else new for new, msg in result]
