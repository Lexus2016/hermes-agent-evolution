"""Small platform adapter value objects for attachments, debounce, and approvals."""
from __future__ import annotations
import asyncio
from dataclasses import dataclass
from typing import Any, Optional
from gateway.platforms.event import MessageEvent

@dataclass
class CachedMedia:
    """Result of caching one attachment's bytes."""
    path: str                 # absolute HOST cache path (same coordinate system as cache_*_from_bytes)
    media_type: str           # MIME type recorded on the MessageEvent
    kind: str                 # "image" | "video" | "audio" | "document"
    display_name: str         # human-readable name for transcript notes

    def context_note(self) -> str:
        """One-line transcript annotation pointing the agent at the file (sandbox-translated at
        render time, never baked into ``path``: ``event.media_urls`` must hold one coordinate
        system so the routed turn can re-home and translate every entry alike, #101134)."""
        from tools.credential_files import to_agent_visible_cache_path
        return f"[{self.kind} '{self.display_name}' saved at: {to_agent_visible_cache_path(self.path)}]"

@dataclass
class TextDebounceState:
    event: MessageEvent
    task: asyncio.Task | None
    first_ts: float
    last_ts: float

    def cancel_timer(self, *, unless: asyncio.Task | None = None) -> None:
        """Cancel the pending flush timer (if live and not ``unless``)."""
        if self.task is not None and self.task is not unless and not self.task.done():
            self.task.cancel()

@dataclass
class ExecApprovalPrompt:
    """One exec-approval prompt, ready for a platform to render natively (see
    ``BasePlatformAdapter.send_exec_approval``). ``actions`` rows are ``(label, choice, style)``
    with ``choice`` in ``once`` / ``session`` / ``always`` / ``deny`` — the vocabulary
    ``tools.approval.resolve_gateway_approval`` accepts — and ``style`` in ``primary`` /
    ``danger`` / ``""``."""
    chat_id: str
    session_key: str
    text: str
    actions: list[tuple[str, str, str]]
    command: str
    description: str
    smart_denied: bool
    metadata: Optional[dict[str, Any]] = None

    @property
    def choices(self) -> list[str]:
        return [choice for _, choice, _ in self.actions]
