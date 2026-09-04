"""Internal metadata attached to durable conversation messages."""

from __future__ import annotations

from time import time as wall_time
from typing import Any, MutableMapping, Optional, TypeVar


# These fields describe Hermes' durable record, not provider-visible message
# content. They must not influence context-pressure decisions.
PERSISTENCE_ONLY_MESSAGE_FIELDS = frozenset({"timestamp"})

_Message = TypeVar("_Message", bound=MutableMapping[str, Any])


def stamp_message_timestamp(
    message: _Message,
    *,
    timestamp: Optional[float] = None,
) -> _Message:
    """Attach a creation timestamp without replacing source-provided time.

    Gateway adapters can supply the platform event time. All other callers use
    the local wall clock at the point the message enters the live transcript.
    Returning the same mapping keeps the helper convenient at append sites.
    """
    if message.get("timestamp") is None:
        message["timestamp"] = wall_time() if timestamp is None else timestamp
    return message


def append_message(
    messages: list[Any],
    message: _Message,
    *,
    timestamp: Optional[float] = None,
) -> _Message:
    """Stamp and append one live transcript message."""
    stamp_message_timestamp(message, timestamp=timestamp)
    messages.append(message)
    return message


# ---------------------------------------------------------------------------
# Message provenance
# ---------------------------------------------------------------------------
# Which channel a message really came from, as opposed to what its `role` says.
# The runtime relabels attacker-reachable content as role="user" in several
# places (background-process stdout and delegation summaries self-posted by
# gateway/wake.py, a compaction summary emitted with role="user", steer text
# extracted out of a tool result), so the role label is not a trust boundary.
#
# Defined here because this is a leaf module: hermes_state_common imports from
# agent.context_compressor, so the constants cannot live on either of those
# without a cycle, and duplicating them is how the two copies drift apart.

MESSAGE_ORIGIN_HUMAN = "human"
MESSAGE_ORIGIN_RUNTIME = "runtime"
MESSAGE_ORIGIN_API = "api"
MESSAGE_ORIGINS = frozenset(
    {MESSAGE_ORIGIN_HUMAN, MESSAGE_ORIGIN_RUNTIME, MESSAGE_ORIGIN_API}
)


def normalize_message_origin(value: Any) -> Optional[str]:
    """Coerce a message ``origin`` to the known set, or to None.

    Fail-closed by construction: anything unrecognised becomes None, and a
    reader must treat None as untrusted. The value set is enforced here rather
    than with a SQL CHECK constraint, because SQLite applies a CHECK added
    through ALTER TABLE to later writes as well, and legacy rows carry NULL.
    """
    if isinstance(value, str) and value in MESSAGE_ORIGINS:
        return value
    return None
