"""Composable ``Durability`` capability for skills/subagents.

Mirrors the Pydantic AI v2.14 durability-capability pattern
(``TemporalDurability`` / ``DBOSDurability`` / ``PrefectDurability``): a
durability backend checkpoints model requests and tool calls inside a
durable-execution context and can resume from a checkpoint after an
interruption. Outside such a context the agent behaves identically (no-op
default).

Hermes already ships monolithic durable execution (Checkpoints v2, gateway
auto-resume). This module is the composable slice: a capability attached per
skill/subagent rather than wrapping the whole agent. Slice B (#1762) wires this
into one real skill. Interface + registry only — nothing in the live workflow
changes until a skill opts in.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional, Protocol, runtime_checkable

logger = logging.getLogger(__name__)


@runtime_checkable
class DurabilityBackend(Protocol):
    """Pluggable durable-execution backend.

    Checkpoints an arbitrary callable so it can be resumed from a recorded
    checkpoint after an interruption. ``resume_from`` returns the recorded
    result when available, else ``None``.
    """

    name: str

    def run(self, fn: Callable[[], Any], checkpoint_id: Optional[str] = None) -> Any:
        """Run ``fn``, checkpointing it if supported; replay from ``checkpoint_id``."""
        ...

    def resume_from(self, checkpoint_id: str) -> Any:
        """Return the recorded result for ``checkpoint_id`` or ``None`` if unknown."""
        ...


@dataclass
class NoOpDurability:
    """Default backend: durable-execution is a no-op (current behavior).

    ``run`` executes the callable immediately; nothing is checkpointed and
    ``resume_from`` always returns ``None``. Fallback keeps behavior
    byte-identical until a skill opts in.
    """

    name: str = "noop"

    def run(self, fn: Callable[[], Any], checkpoint_id: Optional[str] = None) -> Any:
        return fn()

    def resume_from(self, checkpoint_id: str) -> Any:
        return None


@dataclass
class Durability:
    """Composable durability capability attachable to a skill/subagent.

    ``enabled`` distinguishes "opted in with the no-op backend" from "not opted
    in at all".
    """

    backend: DurabilityBackend = field(default_factory=NoOpDurability)
    enabled: bool = False


class MemoryDurabilityRegistry:
    """In-process backend registry with a no-op default.

    ``resolve`` returns the configured backend for a requested type name, or the
    ``NoOpDurability`` when the name is unknown or empty — keeping the default
    path identical for any skill that has not opted in.
    """

    def __init__(self) -> None:
        self._backends: Dict[str, DurabilityBackend] = {}

    def register(self, name: str, backend: DurabilityBackend) -> None:
        self._backends[name] = backend

    def get(self, name: str) -> Optional[DurabilityBackend]:
        return self._backends.get(name)

    def resolve(self, name: Optional[str]) -> DurabilityBackend:
        """Return the named backend, or the no-op default when unset/unknown."""
        if not name:
            return NoOpDurability()
        backend = self._backends.get(name)
        return backend if backend is not None else NoOpDurability()

    def available(self) -> Dict[str, str]:
        return {name: backend.name for name, backend in self._backends.items()}


def default_registry() -> MemoryDurabilityRegistry:
    """Return the process-wide durability backend registry.

    A module-level singleton so opting-in skills share one registry. Tests that
    need isolation construct their own ``MemoryDurabilityRegistry`` instead.
    """
    return _DEFAULT_REGISTRY


_DEFAULT_REGISTRY = MemoryDurabilityRegistry()
