"""Per-chat preference for how an over-cap reply is delivered: split into messages, or as one file.

The gateway must not decide this for the reader. A daily report of 5-9k characters is ~3 Telegram
messages either way, and which shape is better is a matter of taste and of what the reader does with
it (scroll a thread vs. open and keep a document). So the first time a chat receives an over-cap
reply it is asked once, the answer is remembered here, and nothing is asked again.

Deliberately unset by default: until a chat answers, delivery behaves exactly as before (split), so
the ask can never change what an unanswered chat receives. A chat that never taps keeps the old
behaviour forever — silence is not consent, and it is also not a regression.

Best-effort and dependency-free, like ``rich_sent_store``: every operation swallows errors and
degrades to "unset" / no-op, so a corrupt or unwritable store can never break a delivery.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Optional

from utils import atomic_json_write

logger = logging.getLogger(__name__)

# Preference values. ``SPLIT`` is what the code did before this existed.
SPLIT = "split"
DOCUMENT = "document"
_VALUES = {SPLIT, DOCUMENT}

# Bounded so a gateway talking to many chats cannot grow the file without limit; the oldest
# entries are dropped, which only means those chats get asked again.
_MAX_ENTRIES = 2000


def _store_path() -> str:
    from hermes_constants import get_hermes_home  # honors the active profile override
    return os.path.join(str(get_hermes_home()), "state", "long_reply_prefs.json")


def _key(platform: str, chat_id: str) -> str:
    return f"{platform}:{chat_id}"


def _load() -> dict:
    try:
        with open(_store_path(), encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(data: dict) -> None:
    try:
        if len(data) > _MAX_ENTRIES:
            data = dict(list(data.items())[-_MAX_ENTRIES:])
        path = _store_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        atomic_json_write(path, data)
    except (OSError, ValueError) as exc:
        logger.debug("long-reply preference save failed (%s) — preference not persisted", exc)


def get_preference(platform: str, chat_id: str) -> Optional[str]:
    """``SPLIT`` / ``DOCUMENT`` for this chat, or ``None`` when it has never answered."""
    entry = _load().get(_key(platform, chat_id))
    if isinstance(entry, dict):
        value = entry.get("preference")
        return value if value in _VALUES else None
    return entry if entry in _VALUES else None


def set_preference(platform: str, chat_id: str, preference: str) -> bool:
    """Record this chat's answer; ``False`` for an unknown value (nothing is written)."""
    if preference not in _VALUES:
        return False
    data = _load()
    entry = data.get(_key(platform, chat_id))
    asked = bool(entry.get("asked")) if isinstance(entry, dict) else bool(entry)
    data[_key(platform, chat_id)] = {"preference": preference, "asked": asked}
    _save(data)
    return True


def was_asked(platform: str, chat_id: str) -> bool:
    """Whether this chat has already been offered the choice (answered or not)."""
    entry = _load().get(_key(platform, chat_id))
    if isinstance(entry, dict):
        return bool(entry.get("asked")) or entry.get("preference") in _VALUES
    return entry in _VALUES


def mark_asked(platform: str, chat_id: str) -> None:
    """Record that the choice was offered.

    Stamped when the prompt is SENT, not when it is answered: the prompt is a one-time courtesy, and
    re-offering it on every long report because nobody tapped would be worse than never asking.
    """
    data = _load()
    entry = data.get(_key(platform, chat_id))
    preference = entry.get("preference") if isinstance(entry, dict) else (entry if entry in _VALUES else None)
    data[_key(platform, chat_id)] = {"preference": preference, "asked": True}
    _save(data)
