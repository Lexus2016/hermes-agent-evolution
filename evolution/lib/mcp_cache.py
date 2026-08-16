# -*- coding: utf-8 -*-
"""MCP ttlMs / cacheScope result caching (Issue #2486, Slice B, SEP-2549).

Provides deterministic TTL and scope-aware caching for MCP tool and list results.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, Optional, Tuple, Union

logger = logging.getLogger(__name__)


class CacheScope(str, Enum):
    """Scope defining the boundary and lifespan of cached MCP results."""

    SESSION = "session"
    GLOBAL = "global"
    CALL = "call"
    TOOL = "tool"


@dataclass
class CachedResult:
    """Cached MCP result container with TTL and scope metadata."""

    tool_name: str
    params_hash: str
    data: Any
    created_at_ms: float = field(default_factory=lambda: time.time() * 1000.0)
    ttl_ms: Optional[int] = None
    cache_scope: str = CacheScope.SESSION.value
    session_id: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def is_expired(self, current_time_ms: Optional[float] = None) -> bool:
        """Check if cached entry has exceeded its ttlMs."""
        if self.ttl_ms is None or self.ttl_ms <= 0:
            return False
        now_ms = (
            current_time_ms if current_time_ms is not None else (time.time() * 1000.0)
        )
        return (now_ms - self.created_at_ms) > self.ttl_ms

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class MCPResultCache:
    """In-memory TTL and scope-aware result cache for MCP tool calls."""

    def __init__(self) -> None:
        self._entries: Dict[str, CachedResult] = {}

    @staticmethod
    def hash_params(params: Union[Dict[str, Any], Any]) -> str:
        """Generate deterministic hash for tool parameter payload."""
        try:
            serialized = json.dumps(params, sort_keys=True, default=str)
        except Exception:
            serialized = str(params)
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()

    def _make_key(
        self,
        tool_name: str,
        params_hash: str,
        scope: str,
        session_id: Optional[str] = None,
    ) -> str:
        """Construct isolated cache key based on scope and session."""
        scope_str = scope.lower()
        if scope_str == CacheScope.GLOBAL.value:
            return f"global:{tool_name}:{params_hash}"
        sess_part = session_id or "default"
        return f"{scope_str}:{sess_part}:{tool_name}:{params_hash}"

    @staticmethod
    def extract_cache_control(result: Any) -> Tuple[Optional[int], str]:
        """Extract ttlMs and cacheScope annotations from MCP result metadata (SEP-2549)."""
        ttl_ms: Optional[int] = None
        scope: str = CacheScope.SESSION.value

        if isinstance(result, dict):
            # Check top-level or metadata
            meta = (
                result.get("_meta")
                or result.get("meta")
                or result.get("cacheControl")
                or {}
            )
            ttl_val = (
                result.get("ttlMs")
                or result.get("ttl_ms")
                or meta.get("ttlMs")
                or meta.get("ttl_ms")
            )
            scope_val = (
                result.get("cacheScope")
                or result.get("cache_scope")
                or meta.get("cacheScope")
                or meta.get("cache_scope")
            )
            if ttl_val is not None:
                try:
                    ttl_ms = int(ttl_val)
                except (ValueError, TypeError):
                    ttl_ms = None
            if scope_val and isinstance(scope_val, str):
                scope = scope_val.lower()

        return ttl_ms, scope

    def get(
        self,
        tool_name: str,
        params: Any,
        scope: str = CacheScope.SESSION.value,
        session_id: Optional[str] = None,
    ) -> Optional[Any]:
        """Retrieve cached result if present, matching scope, and not expired."""
        params_hash = self.hash_params(params)
        key = self._make_key(tool_name, params_hash, scope, session_id)
        entry = self._entries.get(key)
        if entry is None:
            # Fallback to global scope check if session-level is empty
            if scope != CacheScope.GLOBAL.value:
                global_key = self._make_key(
                    tool_name, params_hash, CacheScope.GLOBAL.value
                )
                entry = self._entries.get(global_key)

        if entry is None:
            return None

        if entry.is_expired():
            del self._entries[key]
            return None

        return entry.data

    def set(
        self,
        tool_name: str,
        params: Any,
        result: Any,
        ttl_ms: Optional[int] = None,
        cache_scope: str = CacheScope.SESSION.value,
        session_id: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> CachedResult:
        """Store result with specified ttlMs and cacheScope."""
        # Check if result itself contains cache control directives
        extracted_ttl, extracted_scope = self.extract_cache_control(result)
        final_ttl = ttl_ms if ttl_ms is not None else extracted_ttl
        final_scope = (
            extracted_scope
            if extracted_scope != CacheScope.SESSION.value
            else cache_scope
        )

        params_hash = self.hash_params(params)
        key = self._make_key(tool_name, params_hash, final_scope, session_id)

        entry = CachedResult(
            tool_name=tool_name,
            params_hash=params_hash,
            data=result,
            ttl_ms=final_ttl,
            cache_scope=final_scope,
            session_id=session_id,
            metadata=metadata or {},
        )
        self._entries[key] = entry
        return entry

    def invalidate(
        self,
        tool_name: Optional[str] = None,
        scope: Optional[str] = None,
        session_id: Optional[str] = None,
    ) -> int:
        """Invalidate cache entries matching given filters."""
        to_delete = []
        for key, entry in self._entries.items():
            if tool_name and entry.tool_name != tool_name:
                continue
            if scope and entry.cache_scope != scope:
                continue
            if (
                session_id
                and entry.session_id != session_id
                and entry.cache_scope != CacheScope.GLOBAL.value
            ):
                continue
            to_delete.append(key)

        for k in to_delete:
            del self._entries[k]
        return len(to_delete)

    def wrap_with_cache(
        self,
        handler_fn: Callable[[str, Dict[str, Any]], Any],
        session_id: Optional[str] = None,
        default_ttl_ms: Optional[int] = None,
    ) -> Callable[[str, Dict[str, Any]], Any]:
        """Wrap a tool execution handler function with automatic result caching."""

        def _cached_handler(name: str, args: Dict[str, Any]) -> Any:
            cached = self.get(name, args, session_id=session_id)
            if cached is not None:
                logger.debug("MCP cache hit for tool '%s'", name)
                return cached

            res = handler_fn(name, args)
            self.set(
                tool_name=name,
                params=args,
                result=res,
                ttl_ms=default_ttl_ms,
                session_id=session_id,
            )
            return res

        return _cached_handler


# Global singleton instance for shared MCP cache
_GLOBAL_MCP_CACHE = MCPResultCache()


def get_global_mcp_cache() -> MCPResultCache:
    """Return the global MCPResultCache singleton."""
    return _GLOBAL_MCP_CACHE
