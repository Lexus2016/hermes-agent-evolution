"""Tool Call Caching Layer for Hermes Agent.

Implements an adaptive caching layer for tool call results, inspired by
ToolCacheAgent research. Provides automatic cache key generation, TTL-based
expiration, and intelligent invalidation rules for stateful operations.

Key features:
- Automatic cache key generation from tool inputs
- TTL-based expiration (configurable per tool)
- Invalidation rules for stateful operations
- Cache hit/miss metrics tracking
- Thread-safe concurrent access
- Memory bounds with LRU eviction

Reference:
    https://openreview.net/forum?id=tX3YcbNa5w (ToolCacheAgent)
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Set, Tuple
from functools import wraps

logger = logging.getLogger(__name__)


# Default TTL values for different tool categories (in seconds)
DEFAULT_TTL_CONFIG = {
    "read_file": 300,          # 5 minutes
    "search_files": 180,       # 3 minutes
    "read_file": 300,          # 5 minutes
    "web_search": 600,         # 10 minutes
    "web_extract": 1800,        # 30 minutes
    "session_search": 600,     # 10 minutes
    # Stateful tools that should not be cached by default
    "terminal": 0,
    "write_file": 0,
    "patch": 0,
    "memory": 0,
    "todo": 0,
}


@dataclass
class CacheEntry:
    """A single cache entry storing a tool result."""
    
    result: str
    cached_at: float
    ttl: int
    tool_name: str
    tool_args_hash: str
    access_count: int = 0
    last_access: float = field(default_factory=time.monotonic)
    
    def is_expired(self) -> bool:
        """Check if this cache entry has expired."""
        if self.ttl <= 0:
            return False  # No expiration
        age = time.monotonic() - self.cached_at
        return age > self.ttl
    
    def record_access(self) -> None:
        """Record an access to this entry for LRU tracking."""
        self.access_count += 1
        self.last_access = time.monotonic()


@dataclass
class CacheStats:
    """Statistics tracking cache performance."""
    
    hits: int = 0
    misses: int = 0
    evictions: int = 0
    invalidations: int = 0
    tool_hit_counts: Dict[str, int] = field(default_factory=dict)
    tool_miss_counts: Dict[str, int] = field(default_factory=dict)
    
    def record_hit(self, tool_name: str) -> None:
        """Record a cache hit."""
        self.hits += 1
        self.tool_hit_counts[tool_name] = self.tool_hit_counts.get(tool_name, 0) + 1
    
    def record_miss(self, tool_name: str) -> None:
        """Record a cache miss."""
        self.misses += 1
        self.tool_miss_counts[tool_name] = self.tool_miss_counts.get(tool_name, 0) + 1
    
    def record_eviction(self) -> None:
        """Record a cache eviction."""
        self.evictions += 1
    
    def record_invalidation(self) -> None:
        """Record a cache invalidation."""
        self.invalidations += 1
    
    def get_hit_rate(self) -> float:
        """Calculate the cache hit rate."""
        total = self.hits + self.misses
        if total == 0:
            return 0.0
        return self.hits / total
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert stats to a dictionary for reporting."""
        return {
            "hits": self.hits,
            "misses": self.misses,
            "evictions": self.evictions,
            "invalidations": self.invalidations,
            "hit_rate": self.get_hit_rate(),
            "tool_hit_counts": self.tool_hit_counts.copy(),
            "tool_miss_counts": self.tool_miss_counts.copy(),
        }


class ToolCache:
    """Thread-safe LRU cache for tool call results."""
    
    def __init__(
        self,
        max_size: int = 1000,
        default_ttl: int = 300,
        ttl_config: Optional[Dict[str, int]] = None,
        max_memory_mb: int = 100,
    ):
        """Initialize the tool cache.
        
        Args:
            max_size: Maximum number of entries to store
            default_ttl: Default TTL for cacheable tools (seconds)
            ttl_config: Per-tool TTL overrides (0 = no caching)
            max_memory_mb: Approximate memory limit in MB
        """
        self._cache: OrderedDict[str, CacheEntry] = OrderedDict()
        self._max_size = max_size
        self._default_ttl = default_ttl
        self._ttl_config = {**DEFAULT_TTL_CONFIG, **(ttl_config or {})}
        self._max_memory_mb = max_memory_mb
        self._lock = threading.RLock()
        self._stats = CacheStats()
        self._invalidation_hooks: Dict[str, Set[str]] = {}
        self._reverse_invalidation: Dict[str, Set[str]] = {}
        self._current_memory_bytes = 0
        
        # Register invalidation rules
        self._register_default_invalidation_rules()
    
    def _register_default_invalidation_rules(self) -> None:
        """Register default invalidation rules for stateful tools."""
        # write_file invalidates read_file results for the same path
        self.add_invalidation_rule("write_file", "read_file")
        self.add_invalidation_rule("patch", "read_file")
        
        # terminal commands that modify files invalidate read results
        self.add_invalidation_rule("terminal", "read_file")
        
        # memory operations invalidate session_search
        self.add_invalidation_rule("memory", "session_search")
        
    def add_invalidation_rule(self, source_tool: str, target_tool: str) -> None:
        """Add an invalidation rule: source_tool invalidates target_tool results.
        
        This is a simple rule-based system. For more complex invalidation,
        you can provide a custom invalidation predicate.
        """
        with self._lock:
            if source_tool not in self._invalidation_hooks:
                self._invalidation_hooks[source_tool] = set()
            self._invalidation_hooks[source_tool].add(target_tool)
            
            if target_tool not in self._reverse_invalidation:
                self._reverse_invalidation[target_tool] = set()
            self._reverse_invalidation[target_tool].add(source_tool)
    
    def generate_cache_key(
        self,
        tool_name: str,
        tool_args: Dict[str, Any],
        effective_task_id: Optional[str] = None,
    ) -> str:
        """Generate a cache key from tool name and arguments.
        
        The key includes:
        - Tool name
        - Normalized arguments (sorted keys, JSON-serialized)
        - Optional task ID for task-specific isolation
        
        Args:
            tool_name: Name of the tool being called
            tool_args: Arguments passed to the tool
            effective_task_id: Optional task ID for isolation
            
        Returns:
            A stable cache key string
        """
        # Normalize arguments for consistent hashing
        normalized_args = self._normalize_args(tool_args)
        
        # Create a deterministic string representation
        key_parts = [
            tool_name,
            json.dumps(normalized_args, sort_keys=True, default=str),
        ]
        
        if effective_task_id:
            # Include task ID for isolation (optional - can be disabled)
            key_parts.append(effective_task_id)
        
        key_string = "|".join(key_parts)
        
        # Hash for shorter keys
        return hashlib.sha256(key_string.encode()).hexdigest()[:32]
    
    def _normalize_args(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """Normalize arguments for consistent cache key generation.
        
        Handles:
        - Sorting dict keys
        - Normalizing whitespace in strings
        - Handling optional fields
        """
        normalized = {}
        
        for key in sorted(args.keys()):
            value = args[key]
            
            if isinstance(value, str):
                # Normalize whitespace in strings (but not all whitespace)
                # Just collapse internal multiple whitespace
                if " " in value or "\n" in value or "\t" in value:
                    import re
                    value = re.sub(r'[ \t]+', ' ', value)
                    value = re.sub(r' *\n *', '\n', value)
            elif isinstance(value, dict):
                value = self._normalize_args(value)
            elif isinstance(value, list):
                # Keep lists as-is for now (order matters for most cases)
                pass
            
            normalized[key] = value
        
        return normalized
    
    def get_ttl_for_tool(self, tool_name: str) -> int:
        """Get the configured TTL for a specific tool.
        
        Returns:
            TTL in seconds, or 0 if tool should not be cached
        """
        return self._ttl_config.get(tool_name, self._default_ttl)
    
    def is_cacheable(self, tool_name: str, tool_args: Dict[str, Any]) -> bool:
        """Determine if a tool call should be cached.
        
        Args:
            tool_name: Name of the tool
            tool_args: Arguments to the tool
            
        Returns:
            True if the call should be cached
        """
        ttl = self.get_ttl_for_tool(tool_name)
        if ttl <= 0:
            return False
        
        # Special checks for specific tools
        if tool_name == "terminal":
            # Don't cache commands that modify state
            cmd = tool_args.get("command", "")
            if any(keyword in cmd for keyword in ["rm", "mv", "cp", "write", ">", "git"]):
                return False
        
        return True
    
    def get(
        self,
        tool_name: str,
        tool_args: Dict[str, Any],
        effective_task_id: Optional[str] = None,
    ) -> Optional[str]:
        """Attempt to retrieve a cached result.
        
        Args:
            tool_name: Name of the tool
            tool_args: Arguments to the tool
            effective_task_id: Optional task ID
            
        Returns:
            Cached result string, or None if not found/expired
        """
        if not self.is_cacheable(tool_name, tool_args):
            return None
        
        cache_key = self.generate_cache_key(tool_name, tool_args, effective_task_id)
        
        with self._lock:
            entry = self._cache.get(cache_key)
            
            if entry is None:
                self._stats.record_miss(tool_name)
                return None
            
            if entry.is_expired():
                # Clean up expired entry
                self._remove_entry(cache_key)
                self._stats.record_miss(tool_name)
                return None
            
            # Move to end (LRU)
            self._cache.move_to_end(cache_key)
            entry.record_access()
            self._stats.record_hit(tool_name)
            
            logger.debug(f"Cache HIT for {tool_name} (key: {cache_key[:8]}...)")
            return entry.result
    
    def set(
        self,
        tool_name: str,
        tool_args: Dict[str, Any],
        result: str,
        effective_task_id: Optional[str] = None,
    ) -> None:
        """Cache a tool result.
        
        Args:
            tool_name: Name of the tool
            tool_args: Arguments to the tool
            result: Result string to cache
            effective_task_id: Optional task ID
        """
        if not self.is_cacheable(tool_name, tool_args):
            return
        
        cache_key = self.generate_cache_key(tool_name, tool_args, effective_task_id)
        ttl = self.get_ttl_for_tool(tool_name)
        
        # Estimate entry size
        entry_size = len(result.encode('utf-8'))
        
        with self._lock:
            # Check if updating existing entry
            existing = self._cache.get(cache_key)
            if existing:
                self._current_memory_bytes -= len(existing.result.encode('utf-8'))
            
            # Enforce memory limit
            self._evict_if_needed(entry_size)
            
            # Create and store entry
            entry = CacheEntry(
                result=result,
                cached_at=time.monotonic(),
                ttl=ttl,
                tool_name=tool_name,
                tool_args_hash=cache_key[:16],
            )
            self._cache[cache_key] = entry
            self._current_memory_bytes += entry_size
            
            # Move to end (most recently used)
            self._cache.move_to_end(cache_key)
            
            logger.debug(f"Cached result for {tool_name} (key: {cache_key[:8]}..., TTL: {ttl}s)")
    
    def invalidate(
        self,
        tool_name: str,
        tool_args: Optional[Dict[str, Any]] = None,
        effective_task_id: Optional[str] = None,
    ) -> int:
        """Invalidate cache entries based on tool execution.
        
        This implements invalidation rules - when a stateful tool runs,
        we invalidate cached results from tools that would be affected.
        
        Args:
            tool_name: Name of the tool that just executed
            tool_args: Arguments the tool was called with
            effective_task_id: Optional task ID
            
        Returns:
            Number of entries invalidated
        """
        count = 0
        
        with self._lock:
            # Find tools that should be invalidated by this tool
            affected_tools = self._invalidation_hooks.get(tool_name, set())
            
            if not affected_tools:
                return 0
            
            # Invalidate all entries from affected tools
            keys_to_remove = []
            for key, entry in self._cache.items():
                if entry.tool_name in affected_tools:
                    # Optional: check for path matching
                    if tool_args and self._should_invalidate_entry(tool_name, tool_args, entry, tool_args):
                        keys_to_remove.append(key)
            
            for key in keys_to_remove:
                self._remove_entry(key)
                count += 1
            
            if count > 0:
                self._stats.record_invalidation()
                logger.debug(f"Invalidated {count} cache entries after {tool_name}")
        
        return count
    
    def _should_invalidate_entry(
        self,
        source_tool: str,
        source_args: Dict[str, Any],
        entry: CacheEntry,
        source_args_full: Dict[str, Any],
    ) -> bool:
        """Determine if a cache entry should be invalidated.
        
        This is a simple implementation. More sophisticated invalidation
        could analyze the actual tool arguments (e.g., matching file paths).
        """
        # For now, invalidate all entries of the affected tool type
        # This is conservative but correct
        return True
    
    def _remove_entry(self, cache_key: str) -> None:
        """Remove a cache entry and update memory tracking."""
        if cache_key in self._cache:
            entry = self._cache.pop(cache_key)
            self._current_memory_bytes -= len(entry.result.encode('utf-8'))
    
    def _evict_if_needed(self, new_entry_size: int) -> None:
        """Evict LRU entries if necessary to maintain limits."""
        max_memory_bytes = self._max_memory_mb * 1024 * 1024
        
        # Evict by count
        while len(self._cache) >= self._max_size:
            self._evict_lru()
        
        # Evict by memory
        while (self._current_memory_bytes + new_entry_size) > max_memory_bytes:
            if not self._cache:
                break
            self._evict_lru()
    
    def _evict_lru(self) -> None:
        """Evict the least recently used cache entry."""
        if self._cache:
            cache_key, _ = self._cache.popitem(last=False)
            self._stats.record_eviction()
            logger.debug(f"Evicted cache entry (key: {cache_key[:8]}...)")
    
    def clear(self) -> None:
        """Clear all cache entries."""
        with self._lock:
            self._cache.clear()
            self._current_memory_bytes = 0
            logger.debug("Cache cleared")
    
    def get_stats(self) -> Dict[str, Any]:
        """Get current cache statistics."""
        with self._lock:
            return self._stats.to_dict()
    
    def get_size_info(self) -> Dict[str, Any]:
        """Get information about cache size."""
        with self._lock:
            return {
                "entry_count": len(self._cache),
                "max_entries": self._max_size,
                "memory_bytes": self._current_memory_bytes,
                "max_memory_bytes": self._max_memory_mb * 1024 * 1024,
                "hit_rate": self._stats.get_hit_rate(),
            }


# Global cache instance
_global_cache: Optional[ToolCache] = None
_cache_lock = threading.Lock()


def get_global_cache() -> ToolCache:
    """Get or create the global tool cache instance."""
    global _global_cache
    
    with _cache_lock:
        if _global_cache is None:
            _global_cache = ToolCache()
        return _global_cache


def reset_global_cache() -> None:
    """Reset the global cache (mainly for testing)."""
    global _global_cache
    
    with _cache_lock:
        _global_cache = None


def cached_tool_call(
    tool_name: str,
    tool_args: Dict[str, Any],
    executor: Callable[[], str],
    effective_task_id: Optional[str] = None,
) -> Tuple[str, bool]:
    """Execute a tool call with caching.
    
    This is the main entry point for the caching layer.
    
    Args:
        tool_name: Name of the tool to call
        tool_args: Arguments to pass to the tool
        executor: Function that executes the tool (if cache miss)
        effective_task_id: Optional task ID for cache isolation
        
    Returns:
        Tuple of (result string, cache_hit boolean)
    """
    cache = get_global_cache()
    
    # Try cache first
    cached_result = cache.get(tool_name, tool_args, effective_task_id)
    if cached_result is not None:
        return cached_result, True
    
    # Cache miss - execute the tool
    result = executor()
    
    # Cache the result
    cache.set(tool_name, tool_args, result, effective_task_id)
    
    # Run invalidation for side effects
    cache.invalidate(tool_name, tool_args, effective_task_id)
    
    return result, False


def get_cache_report() -> str:
    """Generate a human-readable cache performance report."""
    cache = get_global_cache()
    stats = cache.get_stats()
    size_info = cache.get_size_info()
    
    lines = [
        "=== Tool Cache Report ===",
        f"Entries: {size_info['entry_count']}/{size_info['max_entries']}",
        f"Memory: {size_info['memory_bytes'] / 1024 / 1024:.1f}/{size_info['max_memory_bytes'] / 1024 / 1024:.0f} MB",
        f"Hit rate: {stats['hit_rate']:.1%}",
        f"Hits: {stats['hits']}, Misses: {stats['misses']}",
        f"Evictions: {stats['evictions']}, Invalidations: {stats['invalidations']}",
        "",
        "Top tools by hit count:",
    ]
    
    # Sort tools by hit count
    sorted_hits = sorted(stats['tool_hit_counts'].items(), key=lambda x: x[1], reverse=True)
    for tool, count in sorted_hits[:10]:
        lines.append(f"  {tool}: {count}")
    
    return "\n".join(lines)
