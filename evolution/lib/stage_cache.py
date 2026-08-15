# -*- coding: utf-8 -*-
"""Node-level caching for repeated evolution pipeline stages (issue #2434).

Provides deterministic, TTL-aware caching of stage outputs to eliminate
redundant LLM calls across repeated execution cycles (inspired by LangGraph's
CachePolicy pattern).

Features:
- Deterministic cache key generation over canonicalized inputs and stage identifiers.
- Configurable TTL policies per stage (e.g. 24h for research, 7d for analysis).
- File-backed atomic persistence with in-memory fallback.
- Explicit invalidation, expiration pruning, and hit/miss statistics tracking.
- Zero external dependencies.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, TypeVar

__all__ = [
    "CachePolicy",
    "StageCache",
    "compute_stage_cache_key",
    "cache_stage_call",
]

T = TypeVar("T")

# Default TTL durations in seconds
DEFAULT_STAGE_TTLS: dict[str, float] = {
    "research": 24 * 3600.0,  # 24 hours
    "analysis": 7 * 24 * 3600.0,  # 7 days
    "synthesis": 3 * 24 * 3600.0,  # 3 days
    "default": 24 * 3600.0,  # 24 hours
}


@dataclass
class CachePolicy:
    """TTL and invalidation policy for an individual stage or execution node."""

    ttl_seconds: float = 86400.0  # 24 hours
    enabled: bool = True
    salt: str = ""

    @classmethod
    def for_stage(cls, stage_name: str, **overrides: Any) -> CachePolicy:
        """Create a policy configured for a known stage name with defaults."""
        ttl = DEFAULT_STAGE_TTLS.get(stage_name.lower(), DEFAULT_STAGE_TTLS["default"])
        kwargs = {"ttl_seconds": ttl, "enabled": True, **overrides}
        return cls(**kwargs)


@dataclass
class CacheEntry:
    """A single cached record on disk or in memory."""

    key: str
    stage: str
    created_at: float
    ttl_seconds: float
    output: Any
    input_digest: str = ""

    def is_expired(self, current_time: float | None = None) -> bool:
        """Check if this entry has exceeded its TTL."""
        now = current_time if current_time is not None else time.time()
        return (now - self.created_at) > self.ttl_seconds

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CacheEntry:
        return cls(
            key=str(data.get("key", "")),
            stage=str(data.get("stage", "")),
            created_at=float(data.get("created_at", 0.0)),
            ttl_seconds=float(data.get("ttl_seconds", 0.0)),
            output=data.get("output"),
            input_digest=str(data.get("input_digest", "")),
        )


def _canonical_json(data: Any) -> str:
    """Produce deterministic JSON representation for hashing."""
    try:
        return json.dumps(
            data, sort_keys=True, ensure_ascii=False, separators=(",", ":")
        )
    except TypeError:
        return json.dumps(
            str(data), sort_keys=True, ensure_ascii=False, separators=(",", ":")
        )


def compute_stage_cache_key(
    stage: str,
    inputs: Any,
    extra_salt: str = "",
) -> str:
    """Deterministic SHA-256 cache key from stage name, canonical inputs, and salt."""
    hasher = hashlib.sha256()
    hasher.update(stage.strip().lower().encode("utf-8"))
    hasher.update(b"::")
    hasher.update(_canonical_json(inputs).encode("utf-8"))
    if extra_salt:
        hasher.update(b"::")
        hasher.update(extra_salt.strip().encode("utf-8"))
    return hasher.hexdigest()


class StageCache:
    """File-backed or in-memory cache manager for evolution pipeline stages."""

    def __init__(
        self,
        cache_dir: str | Path | None = None,
        *,
        default_policy: CachePolicy | None = None,
    ) -> None:
        self.cache_dir = Path(cache_dir) if cache_dir else None
        if self.cache_dir:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.default_policy = default_policy or CachePolicy()
        self._memory_store: dict[str, CacheEntry] = {}
        self._stats = {"hits": 0, "misses": 0, "writes": 0, "invalidations": 0}

    def _get_file_path(self, key: str) -> Path | None:
        if not self.cache_dir:
            return None
        return self.cache_dir / f"{key}.json"

    def get(
        self,
        stage: str,
        inputs: Any,
        policy: CachePolicy | None = None,
    ) -> Any | None:
        """Retrieve cached output if key exists and is not expired."""
        eff_policy = policy or CachePolicy.for_stage(stage)
        if not eff_policy.enabled:
            self._stats["misses"] += 1
            return None

        key = compute_stage_cache_key(stage, inputs, extra_salt=eff_policy.salt)

        # 1. Check in-memory store
        entry = self._memory_store.get(key)

        # 2. Check filesystem store if not in memory
        if entry is None and self.cache_dir:
            fpath = self._get_file_path(key)
            if fpath and fpath.is_file():
                try:
                    data = json.loads(fpath.read_text(encoding="utf-8"))
                    entry = CacheEntry.from_dict(data)
                    self._memory_store[key] = entry
                except Exception:
                    entry = None

        if entry is None:
            self._stats["misses"] += 1
            return None

        if entry.is_expired():
            self._stats["misses"] += 1
            self.invalidate_key(key)
            return None

        self._stats["hits"] += 1
        return entry.output

    def set(
        self,
        stage: str,
        inputs: Any,
        output: Any,
        policy: CachePolicy | None = None,
    ) -> str:
        """Store output in cache with TTL policy."""
        eff_policy = policy or CachePolicy.for_stage(stage)
        if not eff_policy.enabled:
            return ""

        key = compute_stage_cache_key(stage, inputs, extra_salt=eff_policy.salt)
        input_digest = hashlib.sha256(
            _canonical_json(inputs).encode("utf-8")
        ).hexdigest()[:16]

        entry = CacheEntry(
            key=key,
            stage=stage,
            created_at=time.time(),
            ttl_seconds=eff_policy.ttl_seconds,
            output=output,
            input_digest=input_digest,
        )

        self._memory_store[key] = entry

        if self.cache_dir:
            fpath = self._get_file_path(key)
            if fpath:
                try:
                    temp_path = fpath.with_suffix(".tmp")
                    temp_path.write_text(
                        json.dumps(entry.to_dict(), indent=2, ensure_ascii=False),
                        encoding="utf-8",
                    )
                    temp_path.replace(fpath)
                except Exception:
                    pass

        self._stats["writes"] += 1
        return key

    def invalidate_key(self, key: str) -> bool:
        """Invalidate an explicit cache key."""
        removed = False
        if key in self._memory_store:
            del self._memory_store[key]
            removed = True

        if self.cache_dir:
            fpath = self._get_file_path(key)
            if fpath and fpath.is_file():
                try:
                    fpath.unlink()
                    removed = True
                except Exception:
                    pass

        if removed:
            self._stats["invalidations"] += 1
        return removed

    def invalidate_stage(self, stage: str) -> int:
        """Invalidate all cache entries associated with a specific stage."""
        stage_clean = stage.strip().lower()
        keys_to_remove = [
            k for k, v in self._memory_store.items() if v.stage.lower() == stage_clean
        ]
        for k in keys_to_remove:
            self.invalidate_key(k)

        count = len(keys_to_remove)

        if self.cache_dir:
            try:
                for p in self.cache_dir.glob("*.json"):
                    try:
                        data = json.loads(p.read_text(encoding="utf-8"))
                        if str(data.get("stage", "")).lower() == stage_clean:
                            p.unlink(missing_ok=True)
                            count += 1
                    except Exception:
                        pass
            except Exception:
                pass

        return count

    def prune_expired(self) -> int:
        """Prune all expired entries from memory and disk."""
        now = time.time()
        expired_keys = [k for k, v in self._memory_store.items() if v.is_expired(now)]
        for k in expired_keys:
            self.invalidate_key(k)

        count = len(expired_keys)

        if self.cache_dir:
            try:
                for p in self.cache_dir.glob("*.json"):
                    try:
                        data = json.loads(p.read_text(encoding="utf-8"))
                        entry = CacheEntry.from_dict(data)
                        if entry.is_expired(now):
                            p.unlink(missing_ok=True)
                            count += 1
                    except Exception:
                        pass
            except Exception:
                pass

        return count

    def stats(self) -> dict[str, int]:
        """Return cache hit/miss/write statistics."""
        return dict(self._stats)


def cache_stage_call(
    cache: StageCache,
    stage: str,
    inputs: Any,
    compute_fn: Callable[[], T],
    policy: CachePolicy | None = None,
) -> tuple[T, bool]:
    """Execute compute_fn with stage cache wrapper.

    Returns:
        (result, was_cached)
    """
    cached = cache.get(stage, inputs, policy=policy)
    if cached is not None:
        return cached, True

    result = compute_fn()
    cache.set(stage, inputs, result, policy=policy)
    return result, False
