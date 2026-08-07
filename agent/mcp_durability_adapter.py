"""Durability-aware wrapper for MCP tool calls (#1782 / #1288).

Routes MCP tool-call communication through a :class:`DurabilityBackend` so that
if the process crashes mid-tool-call, the MCP interaction can be replayed /
resumed from checkpoint rather than starting over.

Design:
- Before each MCP tool call, checkpoint ``{tool_name, args, call_id, status:
  "pending"}`` via :meth:`DurabilityBackend.checkpoint` (i.e. ``run`` with a
  deterministic ``checkpoint_id``).
- On resume / replay, if a checkpointed MCP call exists that wasn't completed,
  the adapter re-invokes the underlying tool handler.
- On completion, the checkpoint is marked done (result stored), so a replay
  skips re-execution.

The adapter is a thin wrapper around a *callable* (the MCP tool handler
produced by ``_make_tool_handler``). When no durability backend is configured
(or the no-op backend is used), behavior is byte-identical to calling the
handler directly — the wrapper is a pure passthrough.

This module is deliberately framework-agnostic: it operates on a
``tool_fn(args: dict, **kwargs) -> str`` callable and a
:class:`~agent.durability.DurabilityBackend`, with no dependency on the MCP
SDK or event loop. The live MCP wiring (in ``tools/mcp_tool.py``) can opt in
by wrapping its handler with :func:`with_durability_checkpoint`.
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any, Callable, Dict, Optional

from agent.durability import DurabilityBackend, NoOpDurability

logger = logging.getLogger(__name__)

_CHECKPOINT_PREFIX = "mcp-tool-call"
_PENDING = "pending"
_DONE = "done"


def _checkpoint_id(tool_name: str, call_id: str) -> str:
    """Deterministic checkpoint id: ``mcp-tool-call-<hash>``.

    Hashing (not raw concatenation) keeps the id within the filesystem-safe
    charset enforced by ``FileDurabilityBackend._checkpoint_path`` even when
    ``tool_name`` contains slashes or spaces.
    """
    raw = f"{tool_name}:{call_id}"
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
    return f"{_CHECKPOINT_PREFIX}-{digest}"


class McpDurabilityAdapter:
    """Wraps an MCP tool handler with durability checkpointing.

    Parameters
    ----------
    tool_name:
        The MCP tool name (for logging / checkpoint metadata).
    tool_fn:
        The underlying sync handler: ``fn(args: dict, **kwargs) -> str``.
    backend:
        A :class:`DurabilityBackend`. Defaults to :class:`NoOpDurability` so
        the adapter is a no-op unless a real backend is injected.

    Lifecycle
    ---------
    1. ``call(args, call_id)`` computes the checkpoint id.
    2. If a completed result is already checkpointed → return it immediately
       (replay / resume fast-path).
    3. Otherwise, write a *pending* checkpoint, execute ``tool_fn``, then
       overwrite with the *done* result.
    4. If the process crashes between steps 3-write and 3-execute, the next
       call with the same ``call_id`` sees a pending checkpoint and replays.
    """

    def __init__(
        self,
        tool_name: str,
        tool_fn: Callable[..., str],
        backend: Optional[DurabilityBackend] = None,
    ) -> None:
        self.tool_name = tool_name
        self.tool_fn = tool_fn
        self.backend: DurabilityBackend = backend or NoOpDurability()

    # -- public API --------------------------------------------------------

    def call(self, args: dict, call_id: str, **kwargs: Any) -> str:
        """Execute the MCP tool call with durability checkpointing.

        Returns the tool result string. On replay (if a done checkpoint
        exists for ``call_id``), returns the stored result without
        re-invoking ``tool_fn``.  Extra ``kwargs`` (dispatch metadata such
        as ``task_id``) are forwarded to the underlying handler verbatim.
        """
        cp_id = _checkpoint_id(self.tool_name, call_id)
        done_id = f"{cp_id}-done"

        # Fast-path: a completed checkpoint already exists → replay.
        existing = self.backend.resume_from(done_id)
        if isinstance(existing, dict) and existing.get("status") == _DONE:
            logger.debug(
                "MCP durability: replaying completed checkpoint for %s (%s)",
                self.tool_name,
                call_id,
            )
            result = existing.get("result")
            return result if isinstance(result, str) else str(result)

        # Write pending checkpoint (only meaningful for real backends).
        self._write_checkpoint(cp_id, _PENDING, args, call_id, result=None)

        logger.debug(
            "MCP durability: executing tool %s (call_id=%s)",
            self.tool_name,
            call_id,
        )
        result = self.tool_fn(args, **kwargs)

        # Mark done — store the result so a replay returns it directly.
        self._write_checkpoint(done_id, _DONE, args, call_id, result=result)
        return result

    # -- internals ---------------------------------------------------------

    def _write_checkpoint(
        self,
        cp_id: str,
        status: str,
        args: dict,
        call_id: str,
        result: Optional[str],
    ) -> None:
        """Persist a checkpoint payload via ``backend.run``.

        Uses ``run(fn, checkpoint_id)`` so the backend's own idempotency
        applies: for ``FileDurabilityBackend``, ``run`` stores the payload on
        first call and returns the cached value on subsequent calls with the
        same id (read-on-exist). The ``call()`` method uses distinct ids for
        the pending (``cp_id``) and done (``cp_id-done``) states so both
        are persisted independently.
        """
        payload: Dict[str, Any] = {
            "tool_name": self.tool_name,
            "args": args,
            "call_id": call_id,
            "status": status,
        }
        if result is not None:
            payload["result"] = result
        self.backend.run(lambda: payload, checkpoint_id=cp_id)


def with_durability_checkpoint(
    tool_name: str,
    tool_fn: Callable[..., str],
    backend: Optional[DurabilityBackend] = None,
) -> Callable[..., str]:
    """Convenience factory: return a handler wrapped with durability.

    The returned callable has signature ``fn(args: dict, **kwargs) -> str``,
    matching the MCP tool-handler registry interface (which forwards dispatch
    metadata such as ``task_id``). A ``call_id`` is derived from the args
    hash so replay is deterministic for identical invocations.
    """
    adapter = McpDurabilityAdapter(tool_name, tool_fn, backend)

    def _wrapped(args: dict, **kwargs: Any) -> str:
        call_id = hashlib.sha256(
            json.dumps(args, sort_keys=True, default=str).encode("utf-8"),
        ).hexdigest()[:16]
        return adapter.call(args, call_id, **kwargs)

    return _wrapped
