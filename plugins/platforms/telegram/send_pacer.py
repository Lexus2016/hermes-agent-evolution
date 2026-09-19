"""Outbound pacing for Telegram sends — stop walking into flood control instead of recovering from it.

Three separate delivery defects in this adapter all had the same origin: the bot sends faster than
Telegram allows, gets a penalty of 9-269 seconds, and then some recovery path has to reconstruct
what the reader missed. Reconstruction is where replies lost their tail, interleaved with another
reply, or arrived split mid-word. Pacing removes the penalty instead of improving the recovery.

Telegram publishes no exact numbers, and the ones in circulation are: roughly one message per second
to a single chat, about 20 per minute to a group, and around 30 per second across the whole bot. The
defaults here sit just inside those, and both limits are configurable because a bot that talks to few
chats can afford to be slower and one under load may need the global cap lowered further.

This is deliberately NOT a token bucket per chat: a bucket models a burst allowance, and bursting to
one chat is exactly what earns the penalty. A minimum interval models the real constraint directly.
The global side IS a bucket, because there a short burst is genuinely allowed.

Penalties that still happen are not this class's job — ``_record_send_cooldown`` owns the wait once
Telegram has refused.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Dict

logger = logging.getLogger(__name__)

# Just inside the limits above, leaving room for the edits and typing actions that also count.
DEFAULT_PER_CHAT_INTERVAL = 1.05
DEFAULT_GLOBAL_RATE = 25.0
DEFAULT_GLOBAL_BURST = 25.0
# A chat idle longer than this is dropped from the table; its next send is due immediately anyway.
_STALE_AFTER_SECONDS = 300.0
_PRUNE_AT = 512


class TelegramSendPacer:
    """Per-chat minimum interval plus a process-wide burst budget."""

    def __init__(self, per_chat_interval: float = DEFAULT_PER_CHAT_INTERVAL,
                 global_rate: float = DEFAULT_GLOBAL_RATE,
                 global_burst: float = DEFAULT_GLOBAL_BURST) -> None:
        self.per_chat_interval = max(0.0, float(per_chat_interval))
        self.global_rate = max(0.1, float(global_rate))
        self.global_burst = max(1.0, float(global_burst))
        self._next_allowed: Dict[str, float] = {}
        self._tokens = self.global_burst
        self._last_refill = time.monotonic()
        self._lock = asyncio.Lock()

    def _refill_locked(self, now: float) -> None:
        elapsed = now - self._last_refill
        if elapsed > 0:
            self._tokens = min(self.global_burst, self._tokens + elapsed * self.global_rate)
            self._last_refill = now

    def _prune_locked(self, now: float) -> None:
        if len(self._next_allowed) < _PRUNE_AT:
            return
        cutoff = now - _STALE_AFTER_SECONDS
        for key in [k for k, v in self._next_allowed.items() if v < cutoff]:
            del self._next_allowed[key]

    async def wait_turn(self, chat_key: str) -> float:
        """Sleep until a send to ``chat_key`` fits both budgets; returns the seconds slept.

        Reserves the slot before sleeping, so concurrent senders to the same chat queue behind each
        other rather than all waking at the same instant and re-creating the burst.
        """
        if self.per_chat_interval <= 0 and self.global_rate <= 0:
            return 0.0
        async with self._lock:
            now = time.monotonic()
            self._prune_locked(now)
            self._refill_locked(now)
            # Per-chat: not before this chat's next slot.
            due = max(now, self._next_allowed.get(chat_key, 0.0))
            # Global: not before the burst budget has a token.
            if self._tokens < 1.0:
                due = max(due, now + (1.0 - self._tokens) / self.global_rate)
            # NOT clamped at zero: the deficit is the queue depth. Clamping made every waiter
            # compute the same deadline and wake together, re-creating the burst being paced.
            self._tokens -= 1.0
            self._next_allowed[chat_key] = due + self.per_chat_interval
            wait = due - now
        if wait <= 0:
            return 0.0
        logger.debug("Telegram pacer: holding a send to %s for %.2fs", chat_key, wait)
        await asyncio.sleep(wait)
        return wait

    def try_slot(self, chat_key: str) -> bool:
        """Take a slot if one is free right now; never waits. ``False`` means "skip this one".

        Streaming EDITS use this rather than ``wait_turn``: an edit is a preview of text the next
        tick will show anyway, so delaying it buys nothing and skipping it costs nothing. Sends must
        wait instead — skipping one would drop a message.

        Telegram counts edits and sends against the SAME per-chat allowance, so both go through this
        one budget. Two independent throttles (0.8s for edits, ~1s for sends) added up to more than
        two messages a second to one chat, which is what kept earning the penalties.
        """
        now = time.monotonic()
        self._prune_locked(now)
        self._refill_locked(now)
        if self._next_allowed.get(chat_key, 0.0) > now or self._tokens < 1.0:
            return False
        self._tokens -= 1.0
        self._next_allowed[chat_key] = now + self.per_chat_interval
        return True

    def state(self) -> dict:
        """Read-only snapshot for diagnostics."""
        return {"per_chat_interval": self.per_chat_interval, "global_rate": self.global_rate,
                "tokens": round(self._tokens, 2), "chats_tracked": len(self._next_allowed)}
