"""Platform-neutral delivery outcomes, safe partial tails, and queued-event merging."""
from __future__ import annotations
from dataclasses import dataclass
from typing import Any, Optional, Callable
from gateway.platforms.event import MessageEvent, MessageType

def _append_text(existing: Optional[str], new: Optional[str]) -> str:
    """``existing\\nnew`` when both non-empty; the non-empty one otherwise."""
    return f"{existing}\n{new}" if existing else new

@dataclass
class SendResult:
    """Result of sending a message."""
    success: bool
    message_id: Optional[str] = None
    error: Optional[str] = None
    # Adapter-specific metadata. Contract: Telegram edit-overflow partials set
    # raw_response["partial_overflow"] so the stream consumer sends the missing tail.
    raw_response: Any = None
    retryable: bool = False  # transient connection error — base retries automatically
    retry_after: Optional[float] = None  # server-requested delay (Telegram FloodWait) beats our backoff
    # Extra ids (send order) when a payload was split; ``message_id`` is then the LAST id so
    # later edits target the newest chunk.
    continuation_message_ids: tuple = ()
    # SEND_ERROR_KINDS member (failures only) via :func:`classify_send_error`, so consumers
    # branch without substring-matching ``error``.
    error_kind: Optional[str] = None

def undelivered_tail_after_partial(result: SendResult, sent: str) -> Optional[str]:
    """The not-yet-delivered remainder of ``sent`` when an adapter reported a PARTIAL split send
    (``raw_response["partial_overflow"]``), else ``None``.

    A payload over the platform's cap goes out as several messages, so a refusal on a later chunk
    (typically a flood-control fail-closed) leaves the earlier chunks on screen. Re-sending the
    whole payload duplicates that visible head; dropping it leaves the reader with a reply that
    stops mid-sentence. Both are wrong — resend only what this returns.
    """
    from gateway.platforms.base import BasePlatformAdapter

    raw = getattr(result, "raw_response", None)
    if not (isinstance(raw, dict) and raw.get("partial_overflow")):
        return None
    # Resuming is only sound when non-delivery is CERTAIN. A read/write timeout may have been
    # accepted by the platform after the client gave up (see ``_is_timeout_error``), so treating
    # its "tail" as owed would duplicate content — the very thing this helper exists to avoid.
    # Flood refusals and explicit rejections are definite; these are not.
    if BasePlatformAdapter._is_timeout_error(str(getattr(result, "error", "") or "")):
        return None
    tail = raw.get("undelivered_tail")
    if isinstance(tail, str) and tail.strip() and tail != sent:
        return tail
    prefix = raw.get("delivered_prefix")
    if isinstance(prefix, str) and prefix and sent.startswith(prefix) and len(prefix) < len(sent):
        return sent[len(prefix):].lstrip() or None
    return None

SEND_ERROR_KINDS = frozenset(
    {"too_long", "bad_format", "forbidden", "not_found", "rate_limited", "transient", "unknown"})

_CHAT_LEVEL_NOT_FOUND_SUBSTRINGS = ("chat not found",)

_SUBCHAT_NOT_FOUND_SUBSTRINGS = (
    "message to edit not found", "message to reply not found", "thread not found", "topic_deleted",
    "message_id_invalid")

def _error_blob(exc: Optional[BaseException] = None, error_text: str = "") -> str:
    """Lowercased blob (error_text + str(exc) + exception class name) that both
    send-error classifiers match against — one builder so they can never drift."""
    parts = [error_text] if error_text else []
    if exc is not None:
        parts.extend(p for p in (str(exc), exc.__class__.__name__) if p)
    return " ".join(parts).lower()

def _any_in(blob: str, *needles: str) -> bool:
    return any(n in blob for n in needles)

_SEND_ERROR_CLASSIFIERS: tuple[tuple[str, Callable[[str], bool]], ...] = (
    ("too_long", lambda b: _any_in(b, "message_too_long", "too long", "message is too long")),
    ("bad_format", lambda b: (
        _any_in(b, "can't parse entities", "cant parse entities", "can't find end", "unsupported start tag")
        or ("entity" in b and "parse" in b)
        or ("bad request" in b and "entit" in b))),
    ("forbidden", lambda b: _any_in(
        b, "forbidden", "bot was blocked", "blocked by the user", "user is deactivated",
        "not enough rights", "have no rights", "not a member")),
    ("not_found", lambda b: _any_in(b, *_CHAT_LEVEL_NOT_FOUND_SUBSTRINGS, *_SUBCHAT_NOT_FOUND_SUBSTRINGS)),
    ("rate_limited", lambda b: _any_in(b, "flood", "too many requests", "retry after", "rate limit")),
    ("transient", lambda b: _any_in(b, *_RETRYABLE_ERROR_PATTERNS, "connecttimeout")))

def classify_send_error(exc: Optional[BaseException], error_text: str = "") -> str:
    """Map a send exception / error string to a :data:`SEND_ERROR_KINDS` value; anything
    unrecognized is ``"unknown"`` so an unclassified failure is never mistaken for a benign one."""
    blob = _error_blob(exc, error_text)
    return next((kind for kind, matches in _SEND_ERROR_CLASSIFIERS if matches(blob)), "unknown")

def is_chat_level_not_found(exc: Optional[BaseException] = None, error_text: str = "") -> bool:
    """Whether a ``not_found`` failure means the *whole chat* is gone (only that marks a target
    dead; a deleted topic / edited-away message leaves the parent chat reachable). When both
    markers are present the sub-chat reading wins."""
    blob = _error_blob(exc, error_text)
    return (not _any_in(blob, *_SUBCHAT_NOT_FOUND_SUBSTRINGS)
            and _any_in(blob, *_CHAT_LEVEL_NOT_FOUND_SUBSTRINGS))

class EphemeralReply(str):
    """System-notice reply that auto-deletes after ``ttl_seconds`` on platforms implementing
    ``delete_message`` (others leave it). ``None`` ttl uses ``display.ephemeral_system_ttl``
    (``0`` disables globally). Subclassing ``str`` keeps it transparent to everything that
    treats handler results as text; ``isinstance`` lets the send path schedule deletion."""
    ttl_seconds: Optional[int]

    def __new__(cls, text: str, ttl_seconds: Optional[int] = None):
        instance = super().__new__(cls, text)
        instance.ttl_seconds = ttl_seconds
        return instance

    @property
    def text(self) -> str:
        """The underlying text (explicit form of ``str(reply)``)."""
        return str.__str__(self)

def merge_pending_message_event(pending_messages: dict[str, MessageEvent], session_key: str,
                                event: MessageEvent, *, merge_text: bool = False) -> None:
    """Store or merge a pending event: photo bursts/albums merge into the queued event so the next
    turn sees the whole burst; with ``merge_text`` rapid TEXT follow-ups append instead of
    replace."""
    from gateway.platforms.base import BasePlatformAdapter

    existing = pending_messages.get(session_key)
    if existing:
        existing_type = getattr(existing, "message_type", None)
        existing_is_photo = existing_type == MessageType.PHOTO
        incoming_is_photo = event.message_type == MessageType.PHOTO
        both_photo = existing_is_photo and incoming_is_photo
        incoming_has_media = bool(event.media_urls)

        def _padded_inline_flags(msg: MessageEvent) -> list[Optional[bool]]:
            flags = list(getattr(msg, "media_text_inlined", []) or [])
            return flags + [None] * (len(msg.media_urls) - len(flags))
        incoming_inline_flags: list[Optional[bool]] = []
        if incoming_has_media:
            existing.media_text_inlined = _padded_inline_flags(existing)
            incoming_inline_flags = _padded_inline_flags(event)
        # A photo burst always absorbs; otherwise merge only when media is involved on either
        # side. Captions merge in every absorbing case.
        if both_photo or existing.media_urls or incoming_has_media:
            if both_photo or incoming_has_media:
                existing.media_urls.extend(event.media_urls)
                existing.media_types.extend(event.media_types)
                existing.media_text_inlined.extend(incoming_inline_flags)
            if event.text:
                existing.text = BasePlatformAdapter._merge_caption(existing.text, event.text)
            existing.absorb_reply_expected(event)
            if existing_is_photo or incoming_is_photo:
                existing.message_type = MessageType.PHOTO
            elif existing_type == MessageType.TEXT and event.message_type != MessageType.TEXT:
                existing.message_type = event.message_type
            # Drop the *derived* STT cache (event changed); the echo ledger must survive or
            # notes echo twice.
            for attr in ("_gateway_pending_stt_text", "_gateway_pending_stt_transcripts"):
                if hasattr(existing, attr):
                    delattr(existing, attr)
            return
        both_text = existing_type == MessageType.TEXT and event.message_type == MessageType.TEXT
        if merge_text and both_text:
            if event.text:
                existing.text = _append_text(existing.text, event.text)
            existing.absorb_reply_expected(event)
            return
    pending_messages[session_key] = event

_RETRYABLE_ERROR_PATTERNS = (
    "connecterror", "connectionerror", "connectionreset", "connectionrefused", "connecttimeout",
    "network", "broken pipe", "remotedisconnected", "eoferror")
