#!/usr/bin/env python3
"""
Dual-Pool Memory Store — agent-private exploitation/exploration pools.

First increment of issue #249 ("Profile memory as agent-private dual-pool with
online judge reweighting"). This module provides the *data structure* and the
*reweighting interface*; it deliberately does NOT wire itself into the agent
loop, the system prompt, or any LLM call. Integration with run_agent and the
LLM-as-judge that produces usefulness scores are explicit follow-ups.

Background (arXiv 2605.22721, DecentMem):
  - Exploitation pool: consolidated, repeatedly-validated trajectories. Loaded
    first, small budget.
  - Exploration pool: LLM-generated candidate memories that have not yet earned
    trust. Loaded behind a lower budget, behind the active pool.
  - Online judge reweighting: after each retrieval a usefulness score updates a
    per-memory weight; weights crossing thresholds promote/demote/evict items.

The reweight update here is a fixed-alpha EMA — a tracking heuristic, not a
provably O(log T)-convergent estimator. The paper's regret-bound knob (which
needs a decaying step size) is intentionally out of scope for this increment;
the alpha is exposed as a constructor knob so a future increment can swap in a
decaying schedule without changing callers.

Storage layout (profile-scoped, isolated per profile exactly like MEMORY.md):

    <hermes_home>/memories/active.jsonl      # exploitation pool
    <hermes_home>/memories/candidate.jsonl   # exploration pool

JSON-lines (one item per line) is used instead of the §-delimited .md format
of MemoryStore because each item carries a per-memory weight and hit counter
that the flat format cannot represent. The existing MEMORY.md / USER.md files
are left completely untouched — this is an additive, parallel structure.

Design choices that match the existing tools/memory_tool.py conventions:
  - Plain classes (no dataclass/pydantic), Dict[str, Any] result style.
  - Path resolved dynamically via get_memory_dir() so profile switches are
    always respected (no import-time caching).
  - Atomic temp-file + rename writes via utils.atomic_replace.
  - Content scanned for injection/exfiltration before acceptance.
"""

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

from tools.memory_tool import _scan_memory_content, get_memory_dir
from utils import atomic_replace

logger = logging.getLogger(__name__)

# Pool identifiers.
ACTIVE = "active"
CANDIDATE = "candidate"
_POOLS = (ACTIVE, CANDIDATE)

# Defaults lifted directly from issue #249.
DEFAULT_ACTIVE_BUDGET = 8
DEFAULT_CANDIDATE_BUDGET = 3
DEFAULT_INITIAL_WEIGHT = 0.5
DEFAULT_EVICT_FLOOR = 0.2
DEFAULT_PROMOTE_CEILING = 0.8
DEFAULT_PROMOTE_AFTER = 3
# EMA smoothing for the reweight update. weight = (1-alpha)*weight + alpha*score.
DEFAULT_REWEIGHT_ALPHA = 0.5


class MemoryItem:
    """A single pooled memory: content plus the online-reweighting bookkeeping.

    Plain class (matching MemoryStore's no-dataclass style). ``weight`` is the
    running usefulness estimate in [0, 1]; ``hits`` counts consecutive reweights
    that left a candidate at/above the promotion ceiling (reset whenever it
    falls below).
    """

    __slots__ = ("content", "weight", "hits")

    def __init__(self, content: str, weight: float = DEFAULT_INITIAL_WEIGHT, hits: int = 0):
        self.content = content
        self.weight = weight
        self.hits = hits

    def to_dict(self) -> Dict[str, Any]:
        return {"content": self.content, "weight": self.weight, "hits": self.hits}

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "MemoryItem":
        return cls(
            content=str(data.get("content", "")),
            weight=float(data.get("weight", DEFAULT_INITIAL_WEIGHT)),
            hits=int(data.get("hits", 0)),
        )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        preview = self.content[:40] + ("…" if len(self.content) > 40 else "")
        return f"MemoryItem({preview!r}, weight={self.weight:.2f}, hits={self.hits})"


class DualPoolMemory:
    """Two-pool curated memory with online judge reweighting.

    Exploitation (``active``) and exploration (``candidate``) pools, each a list
    of :class:`MemoryItem`. Retrieval returns the active pool first (capped by
    ``active_budget``), then tops up from the candidate pool (capped by
    ``candidate_budget``), each ordered by descending weight.

    The :meth:`reweight` step is the integration point for the LLM-as-judge:
    callers pass a content→usefulness-score mapping; weights update via EMA and
    items crossing the thresholds are promoted / demoted / evicted. This module
    does NOT call any LLM — producing the scores is the deferred follow-up.

    One instance per profile; pools are persisted under ``get_memory_dir()`` so
    a profile's pools are never read by another profile.
    """

    def __init__(
        self,
        *,
        active_budget: int = DEFAULT_ACTIVE_BUDGET,
        candidate_budget: int = DEFAULT_CANDIDATE_BUDGET,
        evict_floor: float = DEFAULT_EVICT_FLOOR,
        promote_ceiling: float = DEFAULT_PROMOTE_CEILING,
        promote_after: int = DEFAULT_PROMOTE_AFTER,
        reweight_alpha: float = DEFAULT_REWEIGHT_ALPHA,
    ):
        self.active_budget = active_budget
        self.candidate_budget = candidate_budget
        self.evict_floor = evict_floor
        self.promote_ceiling = promote_ceiling
        self.promote_after = promote_after
        self.reweight_alpha = reweight_alpha
        self._pools: Dict[str, List[MemoryItem]] = {ACTIVE: [], CANDIDATE: []}

    # -- persistence -------------------------------------------------------

    @staticmethod
    def _path_for(pool: str) -> Path:
        return get_memory_dir() / f"{pool}.jsonl"

    def load_from_disk(self) -> None:
        """Load both pools from disk. Missing/corrupt lines are skipped."""
        mem_dir = get_memory_dir()
        mem_dir.mkdir(parents=True, exist_ok=True)
        for pool in _POOLS:
            self._pools[pool] = self._read_pool(self._path_for(pool))

    @staticmethod
    def _read_pool(path: Path) -> List[MemoryItem]:
        if not path.exists():
            return []
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError:
            return []
        items: List[MemoryItem] = []
        seen: set[str] = set()
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                logger.warning("Skipping malformed dual-pool line in %s", path.name)
                continue
            item = MemoryItem.from_dict(data)
            if not item.content or item.content in seen:
                continue
            seen.add(item.content)
            items.append(item)
        return items

    def save_to_disk(self, pool: str) -> None:
        """Persist one pool atomically (temp-file + rename)."""
        get_memory_dir().mkdir(parents=True, exist_ok=True)
        self._write_pool(self._path_for(pool), self._pools[pool])

    @staticmethod
    def _write_pool(path: Path, items: List[MemoryItem]) -> None:
        content = "".join(
            json.dumps(item.to_dict(), ensure_ascii=False) + "\n" for item in items
        )
        try:
            fd, tmp_path = tempfile.mkstemp(
                dir=str(path.parent), suffix=".tmp", prefix=".pool_"
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(content)
                    f.flush()
                    os.fsync(f.fileno())
                atomic_replace(tmp_path, path)
            except BaseException:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
                raise
        except OSError as e:
            raise RuntimeError(f"Failed to write dual-pool file {path}: {e}")

    # -- helpers -----------------------------------------------------------

    def _find(self, pool: str, content: str) -> Optional[MemoryItem]:
        for item in self._pools[pool]:
            if item.content == content:
                return item
        return None

    def _add(self, pool: str, content: str, weight: float) -> Dict[str, Any]:
        content = content.strip()
        if not content:
            return {"success": False, "error": "Content cannot be empty."}
        scan_error = _scan_memory_content(content)
        if scan_error:
            return {"success": False, "error": scan_error}
        # An item lives in exactly one pool; dedupe across both.
        if self._find(pool, content) is not None:
            return {"success": True, "message": "Entry already exists (no duplicate added).",
                    "pool": pool}
        other = CANDIDATE if pool == ACTIVE else ACTIVE
        existing_other = self._find(other, content)
        if existing_other is not None:
            self._pools[other].remove(existing_other)
            self.save_to_disk(other)
        self._pools[pool].append(MemoryItem(content, weight=weight))
        self.save_to_disk(pool)
        return {"success": True, "message": "Entry added.", "pool": pool,
                "pool_size": len(self._pools[pool])}

    # -- public API --------------------------------------------------------

    def add_to_active(self, content: str, weight: float = DEFAULT_PROMOTE_CEILING) -> Dict[str, Any]:
        """Add a consolidated, high-trust memory to the exploitation pool.

        Defaults to the promotion ceiling weight since active items are assumed
        already validated.
        """
        return self._add(ACTIVE, content, weight)

    def add_to_candidate(self, content: str, weight: float = DEFAULT_INITIAL_WEIGHT) -> Dict[str, Any]:
        """Add an unvalidated candidate memory to the exploration pool."""
        return self._add(CANDIDATE, content, weight)

    def promote(self, content: str) -> Dict[str, Any]:
        """Move a candidate into the active pool (force-promote)."""
        item = self._find(CANDIDATE, content)
        if item is None:
            return {"success": False, "error": f"No candidate entry matched '{content[:60]}'."}
        self._pools[CANDIDATE].remove(item)
        item.hits = 0
        self._pools[ACTIVE].append(item)
        self.save_to_disk(CANDIDATE)
        self.save_to_disk(ACTIVE)
        return {"success": True, "message": "Entry promoted to active.", "content": item.content}

    def demote(self, content: str) -> Dict[str, Any]:
        """Move an active item back into the candidate pool (force-demote)."""
        item = self._find(ACTIVE, content)
        if item is None:
            return {"success": False, "error": f"No active entry matched '{content[:60]}'."}
        self._pools[ACTIVE].remove(item)
        item.hits = 0
        self._pools[CANDIDATE].append(item)
        self.save_to_disk(ACTIVE)
        self.save_to_disk(CANDIDATE)
        return {"success": True, "message": "Entry demoted to candidate.", "content": item.content}

    def retrieve(
        self,
        active_budget: Optional[int] = None,
        candidate_budget: Optional[int] = None,
    ) -> List[str]:
        """Return memory contents: active pool first, then candidates.

        Each pool is ordered by descending weight and capped by its budget
        (falling back to the configured defaults). The active pool is always
        emitted before any candidate, matching the exploitation-first design.
        """
        a_budget = self.active_budget if active_budget is None else active_budget
        c_budget = self.candidate_budget if candidate_budget is None else candidate_budget

        def top(pool: str, budget: int) -> List[str]:
            if budget <= 0:
                return []
            ordered = sorted(self._pools[pool], key=lambda it: it.weight, reverse=True)
            return [it.content for it in ordered[:budget]]

        return top(ACTIVE, a_budget) + top(CANDIDATE, c_budget)

    def reweight(self, scores: Dict[str, float]) -> Dict[str, Any]:
        """Apply online judge usefulness scores and run promotion/demotion/eviction.

        ``scores`` maps memory content to a usefulness score in [0, 1] — the
        output of the (deferred) LLM-as-judge pass. For each scored item the
        weight is updated by an EMA::

            weight = (1 - alpha) * weight + alpha * score

        Then thresholds are applied:
          - candidate whose weight stays >= promote_ceiling for ``promote_after``
            consecutive reweights is promoted to active;
          - active whose weight drops below ``evict_floor`` is demoted to
            candidate (graceful — never evicted straight from active);
          - candidate whose weight drops below ``evict_floor`` is evicted.

        Scores outside [0, 1] are clamped. Unknown content keys are ignored.
        Returns a summary of the actions taken.
        """
        promoted: List[str] = []
        demoted: List[str] = []
        evicted: List[str] = []
        touched_pools: set[str] = set()

        # 1) Update weights + hit counters for every scored item in both pools.
        for pool in _POOLS:
            for item in self._pools[pool]:
                if item.content not in scores:
                    continue
                score = scores[item.content]
                score = 0.0 if score < 0.0 else 1.0 if score > 1.0 else score
                item.weight = (1.0 - self.reweight_alpha) * item.weight + self.reweight_alpha * score
                touched_pools.add(pool)
                if pool == CANDIDATE:
                    if item.weight >= self.promote_ceiling:
                        item.hits += 1
                    else:
                        item.hits = 0

        # 2) Promote candidates that sustained high weight.
        for item in list(self._pools[CANDIDATE]):
            if item.weight >= self.promote_ceiling and item.hits >= self.promote_after:
                self._pools[CANDIDATE].remove(item)
                item.hits = 0
                self._pools[ACTIVE].append(item)
                promoted.append(item.content)
                touched_pools.update(_POOLS)

        # 3) Demote weak active items back to candidate (don't evict from active).
        # Track the just-demoted items so step 4 gives them a grace round —
        # demotion is meant to be a softer signal than eviction, so an item
        # should never be demoted and evicted in the same reweight pass.
        demoted_items: List[MemoryItem] = []
        for item in list(self._pools[ACTIVE]):
            if item.weight < self.evict_floor:
                self._pools[ACTIVE].remove(item)
                item.hits = 0
                self._pools[CANDIDATE].append(item)
                demoted.append(item.content)
                demoted_items.append(item)
                touched_pools.update(_POOLS)

        # 4) Evict weak candidates (excluding any just demoted this pass).
        for item in list(self._pools[CANDIDATE]):
            if item in demoted_items:
                continue
            if item.weight < self.evict_floor:
                self._pools[CANDIDATE].remove(item)
                evicted.append(item.content)
                touched_pools.add(CANDIDATE)

        for pool in touched_pools:
            self.save_to_disk(pool)

        return {
            "success": True,
            "promoted": promoted,
            "demoted": demoted,
            "evicted": evicted,
            "active_size": len(self._pools[ACTIVE]),
            "candidate_size": len(self._pools[CANDIDATE]),
        }

    # -- inspection --------------------------------------------------------

    def pool_items(self, pool: str) -> List[MemoryItem]:
        """Return a copy of a pool's items (for inspection / CLI surface)."""
        if pool not in _POOLS:
            raise ValueError(f"Unknown pool '{pool}'. Use 'active' or 'candidate'.")
        return list(self._pools[pool])

    def stats(self) -> Dict[str, Any]:
        """Return per-pool sizes and budgets for inspection."""
        return {
            "active_size": len(self._pools[ACTIVE]),
            "candidate_size": len(self._pools[CANDIDATE]),
            "active_budget": self.active_budget,
            "candidate_budget": self.candidate_budget,
            "evict_floor": self.evict_floor,
            "promote_ceiling": self.promote_ceiling,
            "promote_after": self.promote_after,
        }
