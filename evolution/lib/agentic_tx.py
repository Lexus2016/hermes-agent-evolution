# -*- coding: utf-8 -*-
"""Agentic Transaction Slice 1 — transaction envelope primitive (#2762).

Child of #2759 (Agentic Transaction, from arXiv 2608.13900).  Evolution
mutations (skill writes, issue/PR application, memory updates) are not
atomically rollbackable.  This module provides a lightweight, opt-in
transaction envelope around those mutation paths:

- ``begin`` / ``commit`` / ``rollback`` primitives with compensation actions.
- A compensation registry for mutations that cannot be atomically rolled back.

The envelope is **disabled by default**: when ``enabled=False`` (the
default), ``begin`` returns a no-op context and mutations run exactly as
before — no behavior change to existing paths.

Components:

1. **Compensation registry** — maps a mutation key to a compensation
   callable that undoes it (e.g. restore a previous skill body).
2. **Transaction envelope** — ``begin`` snapshots the registry, ``commit``
   clears the pending compensations (the mutation succeeded), and
   ``rollback`` fires every registered compensation in reverse order.

New module, no changes to existing mutation paths.  Diff ≤ 200 lines.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterator, Optional

logger = logging.getLogger(__name__)

__all__ = [
    "CompensationRegistry",
    "TransactionEnvelope",
    "begin",
]

# A compensation undoes one mutation; it takes no args and returns None.
Compensation = Callable[[], None]


@dataclass
class CompensationRegistry:
    """Maps a mutation key to the compensation that undoes it."""

    _compensations: Dict[str, Compensation] = field(default_factory=dict)

    def register(self, key: str, compensation: Compensation) -> None:
        """Register a compensation for a non-rollbackable mutation."""
        self._compensations[key] = compensation

    def unregister(self, key: str) -> None:
        """Drop a compensation (called on commit — the mutation succeeded)."""
        self._compensations.pop(key, None)

    def rollback_all(self) -> None:
        """Fire every compensation in reverse registration order."""
        for key in reversed(list(self._compensations)):
            try:
                self._compensations[key]()
            except Exception as exc:  # noqa: BLE001 - one bad compensation must not block the rest
                logger.warning("compensation for %r failed: %s", key, exc)
        self._compensations.clear()

    def __len__(self) -> int:
        return len(self._compensations)


@dataclass
class TransactionEnvelope:
    """Opt-in transaction envelope around a mutation path.

    ``enabled=False`` (default) makes ``begin`` a no-op context manager so
    existing mutation paths are unchanged.  When enabled, ``rollback`` fires
    all registered compensations; ``commit`` clears them (the mutation
    succeeded and needs no undo).
    """

    registry: CompensationRegistry = field(default_factory=CompensationRegistry)
    enabled: bool = False

    @contextmanager
    def begin(self) -> Iterator["TransactionEnvelope"]:
        """Context manager: on exception, roll back; on success, commit."""
        if not self.enabled:
            yield self
            return
        try:
            yield self
        except BaseException:
            self.rollback()
            raise
        else:
            self.commit()

    def register(self, key: str, compensation: Compensation) -> None:
        """Register a compensation (no-op when disabled)."""
        if self.enabled:
            self.registry.register(key, compensation)

    def commit(self) -> None:
        """Clear pending compensations — the mutation succeeded."""
        self.registry._compensations.clear()

    def rollback(self) -> None:
        """Fire all compensations in reverse order."""
        self.registry.rollback_all()


def begin(enabled: bool = False) -> TransactionEnvelope:
    """Create a transaction envelope (disabled by default)."""
    return TransactionEnvelope(enabled=enabled)
