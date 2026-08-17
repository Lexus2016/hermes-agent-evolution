"""OTel MCP tracing enrichment for tool-execution spans.

OpenTelemetry's MCP semantic conventions (added in v1.39) standardize
tool-layer observability on the ``mcp.method.name``, ``mcp.session.id`` and
``mcp.protocol.version`` span attributes. This module is the single seam that
maps a Hermes tool-execution monitoring event onto those attributes so
``execute_tool`` spans are queryable by any standard OTel backend.

It is additive and content-free: it never raises and never carries anything
beyond the low-cardinality MCP identifiers, so the monitoring plane's
hot-path invariant (``emit()`` must never block or raise) is preserved.
"""

from __future__ import annotations

from typing import Any, Dict

# OTel MCP semantic-convention attribute keys (GenAI semconv, v1.39+).
MCP_METHOD_NAME = "mcp.method.name"
MCP_SESSION_ID = "mcp.session.id"
MCP_PROTOCOL_VERSION = "mcp.protocol.version"

# Map event fields (underscored, matching the monitoring event style) to the
# standard OTel MCP attribute keys.
_FIELD_TO_ATTR = {
    MCP_METHOD_NAME: "mcp_method_name",
    MCP_SESSION_ID: "mcp_session_id",
    MCP_PROTOCOL_VERSION: "mcp_protocol_version",
}


def mcp_span_attrs(ev: Dict[str, Any]) -> Dict[str, Any]:
    """Return MCP span attributes for a tool-execution event.

    Only present fields are returned; an event with no MCP metadata yields
    an empty dict (so ordinary tool events are unaffected). Values are
    coerced to strings and never contain user/session content.
    """
    attrs: Dict[str, Any] = {}
    for attr_key, field in _FIELD_TO_ATTR.items():
        val = ev.get(field)
        if val:
            attrs[attr_key] = str(val)
    return attrs


__all__ = [
    "MCP_METHOD_NAME",
    "MCP_SESSION_ID",
    "MCP_PROTOCOL_VERSION",
    "mcp_span_attrs",
]
