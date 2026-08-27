# -*- coding: utf-8 -*-
"""MCP Stateless 2026-07-28 server/discover RPC and capability cache (#3247, child of #3240).

Implements the server/discover pre-flight RPC for stateless MCP servers:
- Queries server capabilities (tools, resources, prompts, logging, sampling, stateless).
- Caches discovered capabilities with TTL keyed by server URL or server name.
- Allows dispatchers to query capability support before issuing unsupported calls.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

DEFAULT_CAPABILITY_TTL_SECONDS = 3600.0  # 1 hour default


class MCPCapabilityCache:
    """Thread-safe in-memory cache for MCP server capabilities with TTL."""

    def __init__(self, default_ttl: float = DEFAULT_CAPABILITY_TTL_SECONDS) -> None:
        self._lock = threading.Lock()
        self._default_ttl = default_ttl
        self._cache: Dict[str, Dict[str, Any]] = {}

    def get(self, server_key: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            k = str(server_key or "").strip()
            entry = self._cache.get(k)
            if not entry:
                return None
            expires_at = entry.get("expires_at", 0)
            if time.time() > expires_at:
                self._cache.pop(k, None)
                return None
            return dict(entry.get("capabilities", {}))

    def set(
        self,
        server_key: str,
        capabilities: Dict[str, Any],
        ttl_seconds: Optional[float] = None,
    ) -> None:
        with self._lock:
            k = str(server_key or "").strip()
            if not k:
                return
            ttl = ttl_seconds if ttl_seconds is not None else self._default_ttl
            self._cache[k] = {
                "capabilities": dict(capabilities or {}),
                "expires_at": time.time() + max(0.0, float(ttl)),
            }

    def supports(self, server_key: str, capability: str) -> Optional[bool]:
        """Check if a server supports a capability.

        Returns True/False if known, or None if the server capabilities are uncached.
        """
        caps = self.get(server_key)
        if caps is None:
            return None
        cap = str(capability or "").strip().lower()
        if cap in caps:
            val = caps[cap]
            return bool(val) if not isinstance(val, dict) else True
        return False

    def clear(self, server_key: Optional[str] = None) -> None:
        with self._lock:
            if server_key:
                self._cache.pop(str(server_key).strip(), None)
            else:
                self._cache.clear()


CAPABILITY_CACHE = MCPCapabilityCache()


def parse_discover_response(response_data: Any) -> Dict[str, Any]:
    """Parse server/discover RPC response payload into standard capabilities map."""
    if isinstance(response_data, str):
        try:
            response_data = json.loads(response_data)
        except Exception:
            return {}

    if not isinstance(response_data, dict):
        return {}

    result = response_data.get("result") if "result" in response_data else response_data
    if not isinstance(result, dict):
        return {}

    capabilities = result.get("capabilities")
    if isinstance(capabilities, dict):
        out = dict(capabilities)
    else:
        out = dict(result)

    # Normalize standard top-level flags
    if "stateless" not in out and result.get("stateless"):
        out["stateless"] = True
    if "protocolVersion" in result:
        out["protocol_version"] = result["protocolVersion"]

    return out


def discover_server_capabilities(
    server_key: str,
    raw_response: Optional[Any] = None,
    ttl_seconds: Optional[float] = None,
) -> Dict[str, Any]:
    """Record and cache server capabilities from server/discover response."""
    caps = parse_discover_response(raw_response) if raw_response is not None else {}
    if caps:
        CAPABILITY_CACHE.set(server_key, caps, ttl_seconds=ttl_seconds)
    return caps


def is_mcp_capability_supported(server_key: str, capability: str) -> Optional[bool]:
    """Return whether server_key supports capability (True/False/None if unknown)."""
    return CAPABILITY_CACHE.supports(server_key, capability)
