"""Progressive tool disclosure ("tool search"): MCP/plugin tools and a curated set of
event-triggered core tools are replaced in the model-visible array by three bridge tools —
tool_search / tool_describe / tool_call. Invariants: core tools (``toolsets._HERMES_CORE_TOOLS``)
and session-gated GUI toolsets never defer unless named in ``defer``; ANY deferrable tool
activates the bridge (the listing scales with budget, not activation); the catalog is
stateless — rebuilt from the live tool-defs every assembly (a session-keyed one drifts and
silently drops tools); bridge calls route through ``model_tools.handle_function_call``."""

from __future__ import annotations

import functools
import json
import logging
import math
from collections import Counter
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Tuple, Literal

from tools.registry import tool_error
from tools.tool_search_catalog import (
    BRIDGE_TOOL_NAMES,
    CHARS_PER_TOKEN,
    TOOL_CALL_NAME,
    TOOL_DESCRIBE_NAME,
    TOOL_SEARCH_NAME,
    CatalogEntry,
    _fn,
    _listing_group_label,
    _registry_entry,
    _registry_toolset,
    build_catalog,
    build_catalog_listing_with_form,
    search_catalog,
)
from tools.tool_search_validation import (
    normalize_tool_call_entries,
    validate_deferred_call_args,
)
from tools.connector_search import (
    connections_in_scope,
    connector_entries_by_group,
    remote_schemas_for,
)
from tools.tool_gateway.names import CONNECTOR_BATCH_SENTINEL, is_connector_name

logger = logging.getLogger("tools.tool_search")

# Bound the work one bridge call requests. Search is capped at the gateway's
# own limit: the connector search route answers 7 use_cases per request and
# returns HTTP 502 for 8 or more (measured 2026-09-09), and one local call
# maps to one gateway request. Describe has no such remote limit.
_MAX_QUERIES_PER_CALL = 7
_MAX_DESCRIBE_NAMES_PER_CALL = 10

# ── Consecutive-search streak tracking (#1144 / #1373) ───────────────────
_SEARCH_STREAK: Dict[str, int] = {}
_SEARCH_QUERIES: Dict[str, List[str]] = {}

#: Cap on how many previous queries we surface / retain per session.
_PREVIOUS_QUERIES_MAX = 8

#: Sentinel key used when the runtime hands us an empty session id.
_DEFAULT_SESSION_KEY = "__default_session__"


def _streak_key(session_id: Optional[str]) -> Optional[str]:
    """Resolve a session_id to a streak-tracking key.

    Returns the session id unchanged when it is a non-empty string, a stable
    default key when it is an empty string (the production runtime path), and
    ``None`` only for an explicit ``None`` — which opts out of tracking
    entirely (preserving the pure-function unit-test contract).
    """
    if session_id is None:
        return None
    if session_id == "":
        return _DEFAULT_SESSION_KEY
    return session_id


def note_tool_search(session_id: Optional[str], query: str = "") -> int:
    """Increment the consecutive-search streak for ``session_id``; return it."""
    key = _streak_key(session_id)
    if key is None:
        return 0
    _SEARCH_STREAK[key] = _SEARCH_STREAK.get(key, 0) + 1
    if query:
        hist = _SEARCH_QUERIES.setdefault(key, [])
        hist.append(query)
        if len(hist) > _PREVIOUS_QUERIES_MAX:
            del hist[: len(hist) - _PREVIOUS_QUERIES_MAX]
    return _SEARCH_STREAK[key]


def reset_search_streak(session_id: Optional[str]) -> None:
    """Reset the streak — call when the model invokes a discovered tool."""
    key = _streak_key(session_id)
    if key is not None and key in _SEARCH_STREAK:
        _SEARCH_STREAK[key] = 0
        _SEARCH_QUERIES.pop(key, None)


def get_previous_queries(session_id: Optional[str]) -> List[str]:
    """Return the rolling recent-query history for ``session_id`` (copy)."""
    key = _streak_key(session_id)
    if key is None:
        return []
    return list(_SEARCH_QUERIES.get(key, []))


def _fallback_directive(streak: int) -> str:
    """The nudge appended to a ``tool_search`` result when the streak is high."""
    return (
        f"You have run tool_search {streak} times in a row without calling a "
        "discovered tool. Try one of: (a) broaden the query (more general terms), "
        "(b) call tool_describe on a likely candidate to confirm it does what you "
        "need, or (c) proceed without the deferred tool if the core tools suffice."
    )


@dataclass(frozen=True)
class ToolSearchConfig:
    """Resolved, validated tool-search configuration for a single assembly."""

    enabled: str  # "auto" | "on" | "off" — "auto" is an alias of "on" today
    threshold_pct: float  # 0..100
    search_default_limit: int
    max_search_limit: int
    defer_core_toolsets: frozenset[str] = frozenset()
    search_streak_threshold: int = 3
    search_streak_describe_threshold: int = 5
    listing: str = "auto"  # "auto"/"on" = embed the manifest when it fits; "off" = bare bridge
    listing_max_tokens: int = 4000  # budget = min(this, threshold_pct% of context)
    defer_tools: Optional[frozenset] = None

    @property
    def effective_defer_tools(self) -> frozenset:
        return _DEFAULT_DEFERRED_TOOLS if self.defer_tools is None else self.defer_tools

    @classmethod
    def from_raw(cls, raw: Any) -> "ToolSearchConfig":
        """Build from a raw dict / legacy bool / None; every field is clamped and unknown
        values fall back to safe defaults — a config typo must not break the agent."""
        if not isinstance(raw, dict):  # legacy bool / None
            raw = {"enabled": "off" if raw is False else "auto"}
        max_search_limit = _clamped_int(raw.get("max_search_limit"), 25, 1, 50)
        defer_raw = raw.get("defer")
        streak_threshold = max(
            0, min(20, _clamped_int(raw.get("search_streak_threshold"), 3, 0, 20))
        )
        describe_threshold = max(
            0, min(20, _clamped_int(raw.get("search_streak_describe_threshold"), 5, 0, 20))
        )
        return cls(
            enabled=_tri_state(raw.get("enabled", "auto")),
            threshold_pct=max(0.0, min(100.0, _safe_float(raw.get("threshold_pct"), 5.0))),
            search_default_limit=_clamped_int(
                raw.get("search_default_limit"), 5, 1, max_search_limit
            ),
            max_search_limit=max_search_limit,
            defer_core_toolsets=_parse_toolset_list(raw.get("defer_core_toolsets")),
            search_streak_threshold=streak_threshold,
            search_streak_describe_threshold=describe_threshold,
            listing=_tri_state(raw.get("listing", "auto")),
            listing_max_tokens=_clamped_int(raw.get("listing_max_tokens"), 4000, 200, 60000),
            defer_tools=(
                frozenset(str(n).strip() for n in defer_raw if str(n).strip())
                if isinstance(defer_raw, (list, tuple, set))
                else None
            ),
        )


def _parse_toolset_list(value: Any) -> frozenset[str]:
    """Coerce a raw config value into a frozenset of toolset names."""
    if value is None:
        return frozenset()
    if isinstance(value, str):
        items = value.split(",")
    elif isinstance(value, (list, tuple, set, frozenset)):
        items = list(value)
    else:
        return frozenset()
    names = {
        str(item).strip()
        for item in items
        if isinstance(item, str) and str(item).strip()
    }
    return frozenset(names)


_TRI_STATE_ALIASES = {
    "true": "on",
    "1": "on",
    "yes": "on",
    "false": "off",
    "0": "off",
    "no": "off",
}


def _tri_state(value: Any) -> str:
    """Normalize an ``auto``/``on``/``off`` setting (bool-ish aliases accepted)."""
    text = str(value).strip().lower()
    return _TRI_STATE_ALIASES.get(text, text if text in ("auto", "on", "off") else "auto")


def _clamped_int(value: Any, fallback: int, lo: int, hi: int) -> int:
    try:
        n = int(value)
        return max(lo, min(hi, n))
    except (TypeError, ValueError):
        return fallback


def _safe_float(value: Any, fallback: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return fallback


def _config_from_loader(loader_name: str) -> ToolSearchConfig:
    try:
        import hermes_cli.config as _cfg_mod
        tools_cfg = (getattr(_cfg_mod, loader_name)() or {}).get("tools")
        tools_cfg = tools_cfg if isinstance(tools_cfg, dict) else {}
        return ToolSearchConfig.from_raw(tools_cfg.get("tool_search"))
    except Exception:
        return ToolSearchConfig.from_raw(None)


load_config = functools.partial(_config_from_loader, "load_config")
load_config_readonly = functools.partial(_config_from_loader, "load_config_readonly")


def _hermes_core_tools() -> frozenset[str]:
    """Return the raw ``_HERMES_CORE_TOOLS`` set, unfiltered by config."""
    try:
        from toolsets import _HERMES_CORE_TOOLS
        return frozenset(_HERMES_CORE_TOOLS)
    except Exception:
        return frozenset()


_core_tool_names = _hermes_core_tools

# Session-gated GUI toolsets: off ``_HERMES_CORE_TOOLS`` so non-GUI clients never pay
# their schema; once enabled they stay direct unless the deferral list names them.
_DIRECT_SURFACE_TOOLSETS = frozenset({"desktop_ui", "project"})

_DEFAULT_DEFERRED_TOOLS = frozenset({
    "computer_use", "session_search", "image_generate",
    "todo_list", "process_manage", "cronjob_manage",
    # Desktop GUI surface (desktop_ui + project toolsets)
    "drive_preview", "gui_tour", "desktop_preview", "annotate_preview",
    "show_tip", "setup_mcp", "desktop_project", "close_terminal",
    "apply_layout", "read_terminal", "read_window_below", "focus_pane",
})


def _core_tools_in_toolsets(toolset_names: frozenset[str]) -> frozenset[str]:
    """Return the core tools that belong to any of ``toolset_names``."""
    if not toolset_names:
        return frozenset()
    core = _hermes_core_tools()
    if not core:
        return frozenset()
    members: set[str] = set()
    try:
        from toolsets import resolve_toolset
    except Exception:
        resolve_toolset = None
    try:
        from tools.registry import registry
    except Exception:
        registry = None
    for ts in toolset_names:
        if resolve_toolset is not None:
            try:
                members.update(resolve_toolset(ts))
            except Exception:
                pass
        if registry is not None:
            try:
                members.update(registry.get_tool_names_for_toolset(ts))
            except Exception:
                pass
    return frozenset(members & core)


def effective_core_tool_names(
    config: Optional[ToolSearchConfig] = None,
) -> frozenset[str]:
    """Return the set of tool names that must NEVER be deferred."""
    core = _hermes_core_tools()
    if config is None:
        config = load_config()
    opted_in = _core_tools_in_toolsets(getattr(config, "defer_core_toolsets", frozenset()))
    if not opted_in:
        return core
    return frozenset(core - opted_in)


def is_deferrable_tool_name(
    name: str,
    defer_tools: Optional[Any] = None,
    config: Optional[Any] = None,
) -> bool:
    """True if a tool is *eligible* for deferral."""
    if name in BRIDGE_TOOL_NAMES:
        return False

    cfg_obj = None
    if hasattr(defer_tools, "effective_defer_tools"):
        cfg_obj = defer_tools
        defer_tools = cfg_obj.effective_defer_tools
    elif hasattr(config, "effective_defer_tools"):
        cfg_obj = config
        defer_tools = cfg_obj.effective_defer_tools

    # 1. Opted-in core toolsets
    if cfg_obj is not None and getattr(cfg_obj, "defer_core_toolsets", None):
        effective_core = effective_core_tool_names(cfg_obj)
        if name not in effective_core and name in _hermes_core_tools():
            return True

    # 2. Curated/explicit defer set (only checked when defer_tools is explicitly passed)
    if defer_tools is not None and name in defer_tools:
        return True

    # 3. Core tools never defer otherwise
    if name in _hermes_core_tools():
        return False

    # 4. Registry lookup for plugins / MCP
    toolset = _registry_toolset(name)
    return toolset is not None and (
        toolset.startswith("mcp-") or toolset not in _DIRECT_SURFACE_TOOLSETS
    )


def _tool_def_names(tool_defs: Iterable[Dict[str, Any]]) -> Iterable[str]:
    for td in tool_defs:
        yield _fn(td).get("name", "")


def classify_tools(
    tool_defs: List[Dict[str, Any]],
    defer_tools: Optional[Any] = None,
    config: Optional[Any] = None,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Split a tool-defs list into (visible, deferrable)."""
    visible: List[Dict[str, Any]] = []
    deferrable: List[Dict[str, Any]] = []
    for td in tool_defs:
        fn = _fn(td)
        name = fn.get("name", "")
        if name in BRIDGE_TOOL_NAMES:
            continue
        if is_deferrable_tool_name(name, defer_tools=defer_tools, config=config):
            deferrable.append(td)
        else:
            visible.append(td)
    return visible, deferrable


def _deferrable_in(tool_defs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return classify_tools(tool_defs, load_config_readonly().effective_defer_tools)[1]


def estimate_tokens_from_schemas(tool_defs: Iterable[Dict[str, Any]]) -> int:
    """Estimate token cost via chars/4."""
    def _chars(td: Dict[str, Any]) -> int:
        try:
            return len(json.dumps(td, ensure_ascii=False, separators=(",", ":")))
        except (TypeError, ValueError):
            return len(str(td))
    return int(math.ceil(sum(map(_chars, tool_defs)) / CHARS_PER_TOKEN))


def should_activate(
    config: ToolSearchConfig,
    deferrable_tokens: int,
    context_length: Optional[int],
    *,
    connections_granted: bool = False,
) -> bool:
    """True when tool_search should activate."""
    if config.enabled == "off":
        return False
    if deferrable_tokens > 0:
        return True
    return connections_granted


def listing_token_budget(config: ToolSearchConfig, context_length: Optional[int]) -> int:
    pct_leg = (
        int(context_length * (config.threshold_pct / 100.0))
        if context_length and context_length > 0
        else 10_000
    )
    return max(200, min(config.listing_max_tokens, pct_leg))


def _bridge_schema(
    name: str,
    description: str,
    properties: Dict[str, Any],
    required: Optional[List[str]] = None,
) -> Dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required or list(properties.keys()),
            },
        },
    }


_CONNECTIONS_HINT = (
    " Names starting with `connectors__` are tools of remote connector accounts "
    "(Gmail, Linear, Notion, ...); `manage_connections` checks whether an account "
    "is connected and gets the authorization link when it is not.")


def _search_description(
    deferred_count: int,
    listing: Optional[str],
    listing_form: str,
    connections_granted: bool = False,
) -> str:
    count_phrase = (
        f"{deferred_count} tools"
        if deferred_count
        else ("connected services" if connections_granted else "available tools")
    )
    base = (
        f"Search {count_phrase} by intent or capability. Enter 1–7 short queries "
        "(e.g. ['read pdf', 'fetch weather']). Empty queries are ignored; duplicates dedupe."
    )
    if listing_form == "names":
        base += " The available tools are already listed below; search only when unsure."
    elif listing_form == "groups":
        base += " Toolsets with available tools are listed below; search to discover individual tools."
    if connections_granted:
        base += _CONNECTIONS_HINT
    if listing:
        return f"{base}\n\n{listing}"
    return base


def bridge_tool_schemas(
    deferred_count: int,
    listing: Optional[str] = None,
    listing_form: str = "none",
    connections_granted: bool = False,
) -> List[Dict[str, Any]]:
    """Return the three bridge tools: tool_search / tool_describe / tool_call."""
    return [
        _bridge_schema(
            TOOL_SEARCH_NAME,
            _search_description(deferred_count, listing, listing_form, connections_granted),
            {
                "queries": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Search queries, each a few keywords describing one capability (e.g. ['create github issue', 'send slack message']). Searched in parallel; results come back grouped per query. A single string is accepted and treated as one query.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of matches per query. Defaults to 5 and is clamped to the configured maximum (25 by default).",
                },
            },
            ["queries"],
        ),
        _bridge_schema(
            TOOL_DESCRIBE_NAME,
            f"Load the full JSON schemas for tools returned by `{TOOL_SEARCH_NAME}`. "
            f"Required before `{TOOL_CALL_NAME}` if a tool's parameters are unknown. "
            "Batch every schema you need into one call.",
            {
                "names": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Exact tool names (as returned by tool_search). A single string is accepted and treated as one name.",
                },
            },
            ["names"],
        ),
        _bridge_schema(
            TOOL_CALL_NAME,
            "Invoke deferred tools. Takes `calls`, an array of {name, arguments} "
            "— one entry per invocation; a single call is an array of one. "
            "Local tools require one entry per tool_call. Only connectors__ names "
            "may be batched together; mixed and multi-local batches are rejected. "
            "Connector entries execute individually with results in input order. "
            f"Argument shapes match each tool's schema (see `{TOOL_DESCRIBE_NAME}`). "
            "Policy, hooks, and approvals run as for directly-listed tools.",
            {
                "calls": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string", "description": "Exact tool name to invoke."},
                            "arguments": {"type": "object", "description": "Arguments matching the tool schema."},
                        },
                        "required": ["name", "arguments"],
                    },
                    "description": "One local invocation, or one or more connector invocations. Never mix local and connector tools.",
                },
            },
            ["calls"],
        ),
    ]


@dataclass
class AssemblyResult:
    tool_defs: List[Dict[str, Any]]
    activated: bool
    deferred_count: int = 0
    deferred_tokens: int = 0
    threshold_tokens: int = 0
    tier: int = 0
    listing_form: str = "none"


def assemble_tool_defs(
    tool_defs: List[Dict[str, Any]],
    *,
    context_length: Optional[int] = None,
    config: Optional[ToolSearchConfig] = None,
) -> AssemblyResult:
    """Tool-defs the model should see."""
    config = config or load_config()
    incoming = [
        td for td, name in zip(tool_defs, _tool_def_names(tool_defs))
        if name not in BRIDGE_TOOL_NAMES
    ]
    visible, deferrable = classify_tools(incoming, config.effective_defer_tools)
    connections_granted = connections_in_scope(incoming)
    if not deferrable:
        if should_activate(config, 0, context_length, connections_granted=connections_granted):
            return AssemblyResult(
                tool_defs=incoming + bridge_tool_schemas(0, connections_granted=connections_granted),
                activated=True,
                tier=2,
            )
        return AssemblyResult(tool_defs=incoming, activated=False)
    deferrable_tokens = estimate_tokens_from_schemas(deferrable)
    if not should_activate(config, deferrable_tokens, context_length):
        return AssemblyResult(
            tool_defs=incoming,
            activated=False,
            deferred_count=len(deferrable),
            deferred_tokens=deferrable_tokens,
            threshold_tokens=int((context_length or 0) * (config.threshold_pct / 100.0)),
            tier=0,
        )
    listing, listing_form = None, "none"
    listing_budget = listing_token_budget(config, context_length)
    if config.listing != "off":
        listing, listing_form = build_catalog_listing_with_form(
            deferrable, max_tokens=listing_budget
        )
    bridge = bridge_tool_schemas(
        len(deferrable),
        listing=listing,
        listing_form=listing_form,
        connections_granted=connections_granted,
    )
    tier = 1 if listing_form in ("full", "names", "mixed") else 2
    logger.info(
        "tool_search activated (tier %d): %d core/visible tools kept, %d deferred "
        "(~%d tokens), listing %s (budget ~%d tokens)",
        tier, len(visible), len(deferrable), deferrable_tokens, listing_form, listing_budget,
    )
    return AssemblyResult(
        tool_defs=visible + bridge,
        activated=True,
        deferred_count=len(deferrable),
        deferred_tokens=deferrable_tokens,
        threshold_tokens=listing_budget,
        tier=tier,
        listing_form=listing_form,
    )


def is_bridge_tool(name: str) -> bool:
    return name in BRIDGE_TOOL_NAMES


def _clip_description(text: str, cap: int = 500) -> str:
    cleaned = " ".join(str(text or "").split())
    return (cleaned[: cap - 3] + "...") if len(cleaned) > cap else cleaned


def _shared_tool_record(entry: CatalogEntry) -> Dict[str, Any]:
    try:
        fn = (entry.schema.get("function") or {}) if isinstance(entry.schema, dict) else {}
        params = fn.get("parameters") or {}
        req = params.get("required") or []
    except Exception:
        req = []
    return {
        "source": entry.source,
        "source_name": entry.source_name,
        "description": _clip_description(entry.description or ""),
        "required": [
            r[:64]
            for r in (req if isinstance(req, list) else [])
            if isinstance(r, str)
        ][:32],
    }


def _available_source_summary(catalog: List[CatalogEntry]) -> List[Dict[str, Any]]:
    counts = Counter(_listing_group_label(entry.source_name) for entry in catalog)
    return [{"name": name, "tool_count": counts[name]} for name in sorted(counts)]


def _string_list_arg(
    args: Dict[str, Any],
    key: str,
    *,
    dedupe: bool,
    max_items: int,
    retry_hint: str,
) -> Tuple[Optional[List[str]], Optional[str]]:
    raw = args.get(key)
    raw = [raw] if isinstance(raw, str) else raw
    if not isinstance(raw, list):
        return None, tool_error(f"{key} is required and must be an array of strings")
    out: List[str] = []
    for item in raw:
        text = str(item or "").strip()
        if text and (not dedupe or text not in out):
            out.append(text)
    if not out:
        return None, tool_error(f"{key} is required and must contain at least one non-empty string")
    if len(out) > max_items:
        return None, tool_error(f"too many {key}: {len(out)} > max {max_items}. {retry_hint}")
    return out, None


# ── Search catalog cache (#140) ──────────────────────────────────────────
_search_catalog_cache: dict[tuple[str, str], tuple[List[Dict[str, Any]], List[CatalogEntry]]] = {}
_SEARCH_CATALOG_CACHE_MAX = 16


def _toolset_signature(tool_defs: List[Dict[str, Any]]) -> str:
    names = sorted(
        (td.get("function") or {}).get("name", "")
        for td in tool_defs
        if (td.get("function") or {}).get("name")
    )
    return "|".join(names)


def _search_catalog_key(
    tool_defs: List[Dict[str, Any]], config: ToolSearchConfig
) -> tuple[str, str]:
    sig = _toolset_signature(tool_defs)
    cfg = "|".join(
        str(getattr(config, f, ""))
        for f in ("enabled", "threshold_pct", "defer_core_toolsets")
    )
    return (sig, cfg)


def _build_search_catalog(
    tool_defs: List[Dict[str, Any]], config: ToolSearchConfig
) -> tuple[List[Dict[str, Any]], List[CatalogEntry]]:
    key = _search_catalog_key(tool_defs, config)
    cached = _search_catalog_cache.get(key)
    if cached is not None:
        return cached
    deferrable = _deferrable_in(tool_defs)
    build_fn = globals().get("build_catalog", build_catalog)
    catalog = build_fn(deferrable)
    if len(_search_catalog_cache) >= _SEARCH_CATALOG_CACHE_MAX:
        _search_catalog_cache.clear()
    _search_catalog_cache[key] = (deferrable, catalog)
    return deferrable, catalog


def clear_search_catalog_cache() -> None:
    """Drop the search-catalog cache."""
    _search_catalog_cache.clear()


def _degraded_search_response(
    queries: List[str],
    tool_defs: List[Dict[str, Any]],
    limit: int,
    exc: BaseException,
) -> str:
    names = sorted(
        (td.get("function") or {}).get("name", "")
        for td in tool_defs
        if (td.get("function") or {}).get("name")
        and (td.get("function") or {}).get("name") not in BRIDGE_TOOL_NAMES
    )
    results = []
    for q in queries:
        ql = q.lower()
        matches = [n for n in names if ql in n.lower()][:limit]
        results.append({"query": q, "matches": matches})
    return json.dumps(
        {
            "queries": queries,
            "total_available": len(names),
            "results": results,
            "tools": {},
            "degraded": True,
            "degraded_reason": f"catalog build failed: {exc!r}",
        },
        ensure_ascii=False,
    )


def dispatch_tool_search(
    args: Dict[str, Any],
    *,
    current_tool_defs: List[Dict[str, Any]],
    config: Optional[ToolSearchConfig] = None,
    connector_search: Optional[Any] = None,
    session_id: Optional[str] = None,
) -> str:
    """Execute the ``tool_search`` bridge tool."""
    config = config or load_config()
    if session_id is None:
        session_id = args.get("session_id")
    search_args = args
    if args.get("queries") is None and args.get("query") is not None:
        # A bare ``query`` string (or list) is an understandable model slip;
        # treat it as ``queries``.
        search_args = {**args, "queries": args.get("query")}
    queries, err = _string_list_arg(
        search_args,
        "queries",
        dedupe=False,
        max_items=_MAX_QUERIES_PER_CALL,
        retry_hint="Retry with fewer, more targeted queries.",
    )
    if err:
        return err
    raw_limit = args.get("limit")
    limit = (
        config.search_default_limit
        if raw_limit is None
        else _clamped_int(raw_limit, config.search_default_limit, 1, config.max_search_limit)
    )

    try:
        deferrable, catalog = _build_search_catalog(current_tool_defs, config)
    except Exception as exc:
        return _degraded_search_response(queries, current_tool_defs, limit, exc)

    remote_entries: List[List[CatalogEntry]] = [[] for _ in queries]
    if connections_in_scope(current_tool_defs):
        remote_entries = connector_entries_by_group(queries, connector_search=connector_search)
    results: List[Dict[str, Any]] = []
    tools_map: Dict[str, Dict[str, Any]] = {}
    available_sources = _available_source_summary(catalog) if catalog else []
    all_hits: List[CatalogEntry] = []
    for position, query in enumerate(queries):
        corpus = catalog + remote_entries[position]
        hits = search_catalog(corpus, query, limit=limit)
        # #1137 — optional listwise rerank over BM25 top-k. Config-gated via
        # ``skill_routing.listwise_rerank`` (off by default). Fail-open.
        try:
            from agent.skill_routing import maybe_rerank_hits
            hits = maybe_rerank_hits(query, hits)
        except Exception:
            pass
        all_hits.extend(hits)
        for h in hits:
            tools_map.setdefault(h.name, _shared_tool_record(h))
        matches = [h.name for h in hits]
        group: Dict[str, Any] = {"query": query, "matches": matches}
        if not matches and catalog:
            group["available_sources"] = available_sources
            group["hint"] = (
                "This query returned no lexical matches, but the sources above "
                "are connected and their tools remain available. Retry "
                "tool_search with the service name plus a concrete action or "
                "object before concluding the capability is unavailable."
            )
        results.append(group)
    remote_count = sum(1 for name in tools_map if is_connector_name(name))
    result: Dict[str, Any] = {
        "queries": queries,
        "total_available": len(catalog) + remote_count,
        "results": results,
        "tools": tools_map,
    }

    threshold = getattr(config, "search_streak_threshold", 3)
    describe_threshold = getattr(config, "search_streak_describe_threshold", 5)
    if threshold and threshold > 0:
        query_repr = ", ".join(queries)
        streak = note_tool_search(session_id, query=query_repr)
        if streak >= threshold:
            result["fallback_directive"] = _fallback_directive(streak)
            result["full_tool_list"] = [
                (td.get("function") or {}).get("name", "")
                for td in deferrable
                if (td.get("function") or {}).get("name")
            ]
            prev = get_previous_queries(session_id)
            if prev:
                result["previous_queries"] = (
                    prev[:-1] if prev and prev[-1] == query_repr else list(prev)
                )
            if (
                describe_threshold
                and describe_threshold > 0
                and streak >= max(threshold, describe_threshold)
                and all_hits
            ):
                top = all_hits[0]
                top_def = next(
                    (
                        td
                        for td in deferrable
                        if (td.get("function") or {}).get("name") == top.name
                    ),
                    None,
                )
                if top_def:
                    fn = top_def.get("function") or {}
                    result["auto_describe"] = {
                        "name": top.name,
                        "description": fn.get("description", ""),
                        "parameters": fn.get("parameters", {}),
                    }

    return json.dumps(result, ensure_ascii=False)


# ── tool_describe caching & fuzzy matching (#978, #107, #1015, #2309) ────
_describe_cache: Dict[Tuple[str, str], str] = {}
_DESCRIBE_CACHE_MAX = 64


def clear_describe_cache() -> None:
    """Clear the tool_describe result cache (#1015)."""
    _describe_cache.clear()


def _fuzzy_tool_names(query: str, available: List[str], limit: int = 3) -> List[str]:
    """Return up to ``limit`` tool names closest to ``query`` by substring / edit-distance (#978)."""
    q = query.lower()
    if not q or not available:
        return []
    sub = [n for n in available if q in n.lower()]
    if sub:
        return sorted(sub, key=len)[:limit]

    def _dist(a: str, b: str) -> int:
        a, b = a.lower(), b.lower()
        if len(a) < len(b):
            a, b = b, a
        if not b:
            return len(a)
        prev = list(range(len(b) + 1))
        for i, ca in enumerate(a, 1):
            cur = [i]
            for j, cb in enumerate(b, 1):
                cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
            prev = cur
        return prev[-1]

    scored = sorted(available, key=lambda n: _dist(q, n))[:limit]
    return [n for n in scored if _dist(q, n) <= max(3, len(q) // 3)]


def _tool_schema_payload(
    tool_defs: List[Dict[str, Any]], name: str
) -> Optional[Dict[str, Any]]:
    """Return describe payload if name is in tool_defs (#107)."""
    for td in tool_defs:
        fn = _fn(td)
        if fn.get("name") == name:
            return {
                "name": name,
                "description": fn.get("description", ""),
                "parameters": fn.get("parameters", {}),
            }
    return None


def _dispatch_tool_describe_inner(
    args: Dict[str, Any],
    name: str,
    current_tool_defs: List[Dict[str, Any]],
    config: "ToolSearchConfig",
) -> str:
    """Inner logic for dispatch_tool_describe, separated for caching."""
    if not is_deferrable_tool_name(name, config=config):
        payload = _tool_schema_payload(current_tool_defs, name)
        if payload is not None:
            return json.dumps(payload, ensure_ascii=False)
        _, deferrable = classify_tools(current_tool_defs, config=config)
        available_names = [
            _fn(td).get("name", "") for td in deferrable
        ]
        suggestions = _fuzzy_tool_names(name, available_names)
        if suggestions:
            return json.dumps(
                {
                    "error": (
                        f"'{name}' is not a deferrable tool. Did you mean one of: "
                        f"{', '.join(suggestions)}? Use the exact name with "
                        f"tool_describe or tool_call."
                    ),
                    "suggestions": suggestions,
                    "reason": "not_deferrable",
                    "recovery": (
                        "This tool is not in the deferred set — it may already be in "
                        "your active toolset (call it directly) or it may be misspelled. "
                        "Use a suggested name with tool_describe or tool_call, or re-run "
                        "tool_search to list available deferred tools."
                    ),
                },
                ensure_ascii=False,
            )
        return json.dumps(
            {
                "error": (
                    f"'{name}' is not a deferrable tool. If you see it in the tools list "
                    "already, call it directly; otherwise check the spelling against tool_search."
                ),
                "reason": "not_deferrable",
                "recovery": (
                    "This tool is not in the deferred set. If it is in your active "
                    "toolset, call it directly. Otherwise re-run tool_search to find "
                    "the correct name — do NOT retry tool_describe with the same name."
                ),
            },
            ensure_ascii=False,
        )

    _, deferrable = classify_tools(current_tool_defs, config=config)
    for td in deferrable:
        fn = _fn(td)
        if fn.get("name") == name:
            return json.dumps(
                {
                    "name": name,
                    "description": fn.get("description", ""),
                    "parameters": fn.get("parameters", {}),
                },
                ensure_ascii=False,
            )
    available_names = [_fn(td).get("name", "") for td in deferrable]
    suggestions = _fuzzy_tool_names(name, available_names)
    if suggestions:
        return json.dumps(
            {
                "error": (
                    f"'{name}' is not currently available. Did you mean one of: "
                    f"{', '.join(suggestions)}? Use the exact name with tool_describe "
                    f"or tool_call."
                ),
                "suggestions": suggestions,
                "reason": "not_available",
                "recovery": (
                    "The tool name is deferrable but not in the current toolset scope. "
                    "Use a suggested name, or re-run tool_search to refresh the deferred "
                    "catalog — do NOT retry tool_describe with the same name unchanged."
                ),
            },
            ensure_ascii=False,
        )
    return json.dumps(
        {
            "error": f"'{name}' is not currently available. Re-run tool_search to refresh.",
            "reason": "not_available",
            "recovery": (
                "The tool name is deferrable but not registered in the current session. "
                "Re-run tool_search to refresh the deferred catalog, then use the exact "
                "name returned — do NOT retry tool_describe with the same name."
            ),
        },
        ensure_ascii=False,
    )


def _dispatch_tool_describe_batched(
    args: Dict[str, Any],
    *,
    current_tool_defs: List[Dict[str, Any]],
    config: Optional[ToolSearchConfig] = None,
    connector_describe: Optional[Any] = None,
) -> str:
    config = config or load_config_readonly()
    names, err = _string_list_arg(
        args,
        "names",
        dedupe=True,
        max_items=_MAX_DESCRIBE_NAMES_PER_CALL,
        retry_hint="Retry with fewer names per call.",
    )
    if err:
        return err
    deferrable = _deferrable_in(current_tool_defs)
    by_name = {
        name: _fn(td)
        for td, name in zip(deferrable, _tool_def_names(deferrable))
        if name
    }
    remote_schemas = remote_schemas_for(names, current_tool_defs, connector_describe)

    tools: Dict[str, Dict[str, Any]] = {}
    not_found: List[str] = []
    errors: Dict[str, str] = {}
    for name in names:
        fn = by_name.get(name)
        remote_fn = remote_schemas.get(name)
        if fn is not None:
            tools[name] = {
                "description": fn.get("description", ""),
                "parameters": fn.get("parameters", {}),
            }
        elif isinstance(remote_fn, dict):
            tools[name] = {
                "description": str(remote_fn.get("description", "")),
                "parameters": remote_fn.get("parameters", {}),
            }
        elif is_connector_name(name):
            not_found.append(name)
        elif _registry_entry(name) is not None and not is_deferrable_tool_name(
            name, config=config
        ):
            errors[name] = (
                f"'{name}' is not a deferrable tool. If you see it in the tools list "
                "already, call it directly; otherwise check the spelling against tool_search."
            )
        else:
            not_found.append(name)
    result: Dict[str, Any] = {"tools": tools}
    if not_found:
        result["not_found"] = not_found
        result["hint"] = "Names in not_found are not currently available. Re-run tool_search to refresh."
    if errors:
        result["errors"] = errors
    return json.dumps(result, ensure_ascii=False)


def dispatch_tool_describe(
    args: Dict[str, Any],
    *,
    current_tool_defs: List[Dict[str, Any]],
    config: Optional[ToolSearchConfig] = None,
    connector_describe: Optional[Any] = None,
) -> str:
    """Execute the ``tool_describe`` bridge tool. Returns a JSON string."""
    if args.get("names") is not None:
        return _dispatch_tool_describe_batched(
            args,
            current_tool_defs=current_tool_defs,
            config=config,
            connector_describe=connector_describe,
        )
    if config is None:
        config = load_config()
    name = str(args.get("name") or "").strip()
    if not name:
        return json.dumps({"error": "name is required"}, ensure_ascii=False)

    sig = _toolset_signature(current_tool_defs)
    cache_key = (name, sig)
    cached = _describe_cache.get(cache_key)
    if cached is not None:
        return cached

    result = _dispatch_tool_describe_inner(args, name, current_tool_defs, config)
    if '"error"' not in result:
        if len(_describe_cache) >= _DESCRIBE_CACHE_MAX:
            _oldest_key = next(iter(_describe_cache))
            del _describe_cache[_oldest_key]
        _describe_cache[cache_key] = result
    return result


def scoped_deferrable_names(tool_defs: List[Dict[str, Any]]) -> frozenset[str]:
    defer_tools = load_config_readonly().effective_defer_tools
    return frozenset(
        name for td, name in zip(tool_defs, _tool_def_names(tool_defs))
        if is_deferrable_tool_name(name, defer_tools)
    )


# ── Non-deferrable tool_call alternatives & error (#1392 / #1786 / #1307) ────────
# When an agent (especially in a subagent/cron context where terminal is
# unavailable per #1307) tries to invoke a core tool via tool_call, the
# generic "is not a deferrable tool" message gave no recovery guidance and
# the agent retried the same pattern in a loop.
_CORE_TOOL_ALTERNATIVES: Dict[str, str] = {
    "terminal": (
        "terminal is not available in this environment. "
        "Use search_files for finding files, read_file for reading file contents, "
        "patch for editing files, write_file for creating files, or delegate_task "
        "to spawn a subagent that has terminal access."
    ),
    "execute_code": (
        "execute_code is not available in this environment. "
        "Use delegate_task to spawn a subagent that has code execution access, "
        "or use terminal if available."
    ),
    "browser_navigate": (
        "browser_navigate is not available in this environment. "
        "Use web_search for search queries, web_extract for fetching page content, "
        "or delegate_task to spawn a subagent that has browser access."
    ),
}


def _non_deferrable_error(name: str, config: Optional[ToolSearchConfig] = None) -> str:
    """Build an actionable error message for a non-deferrable tool_call attempt."""
    lower = name.lower()

    if lower in _CORE_TOOL_ALTERNATIVES:
        effective = effective_core_tool_names(config)
        if name in effective:
            return (
                f"'{name}' is a core tool, not a deferrable tool. "
                "Call it directly — it is already in your tools list. "
                "Do not use tool_call for core tools."
            )
        return (
            f"'{name}' is not a deferrable tool and is not available in this "
            f"environment. {_CORE_TOOL_ALTERNATIVES[lower]}"
        )

    effective = effective_core_tool_names(config)
    if name in effective:
        return (
            f"'{name}' is a core tool, not a deferrable tool. "
            f"Call '{name}' directly — it is already in your tools list. "
            "Do not use tool_call for core tools."
        )
    return (
        f"'{name}' is not a deferrable tool. If '{name}' appears in the "
        "model-facing tools list already, call it directly instead of via "
        "tool_call. If it is not in your tools list, check the spelling or "
        "use tool_search to find available deferred tools."
    )


def resolve_underlying_call(
    args: Dict[str, Any], config: Optional[ToolSearchConfig] = None
) -> Tuple[Optional[str], Dict[str, Any], Optional[str]]:
    """Parse a ``tool_call`` invocation into (underlying_name, args, error_msg)."""
    entries, err = normalize_tool_call_entries(args)
    if err:
        return None, {}, err

    if len(entries) > 1 and any(not is_connector_name(e["name"]) for e in entries):
        return None, {}, (
            "Local tools require one entry per tool_call; mixed and multi-local batches are not supported."
        )
    if is_connector_name(entries[0]["name"]):
        return CONNECTOR_BATCH_SENTINEL, {"calls": entries}, None

    name = entries[0]["name"]
    raw_args = entries[0]["arguments"]
    defer_cfg = config if config is not None else load_config_readonly()
    if not is_deferrable_tool_name(name, config=defer_cfg):
        return None, {}, _non_deferrable_error(name, config)
    return name, raw_args, None


# ── Native tool argument validation contract ─────────────────────────────
_SCHEMA_PY_TYPES = {
    "string": (str,),
    "number": (int, float),
    "integer": (int,),
    "boolean": (bool,),
    "array": (list,),
    "object": (dict,),
}


def _check_type(value: Any, type_str: str) -> bool:
    """Check whether *value* matches the JSON Schema *type_str*."""
    if type_str == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    py_types = _SCHEMA_PY_TYPES.get(type_str)
    if py_types is None:
        return True
    return isinstance(value, py_types)


def validate_tool_args(
    name: str,
    args: Dict[str, Any],
    schema: Optional[dict] = None,
) -> Tuple[bool, Optional[str]]:
    """Validate *args* against a tool's OpenAI-format parameter *schema*."""
    if not schema:
        return True, None
    params = schema.get("parameters") or {}
    properties = params.get("properties") or {}
    required = params.get("required") or []

    if not isinstance(args, dict):
        return False, f"Arguments for '{name}' must be an object"

    for req in required:
        if req not in args:
            return False, f"Missing required parameter '{req}' for tool '{name}'"
        if args[req] is None:
            prop = properties.get(req) or {}
            prop_type = prop.get("type")
            is_nullable = (
                prop.get("nullable") is True
                or prop_type == "null"
                or (isinstance(prop_type, list) and "null" in prop_type)
            )
            if not is_nullable:
                return False, f"Missing required parameter '{req}' for tool '{name}'"

    for key, value in args.items():
        if value is None:
            continue
        prop = properties.get(key)
        if not prop:
            continue
        expected_types = prop.get("type")
        if not expected_types:
            continue
        if isinstance(expected_types, str):
            expected_types = [expected_types]
        if not any(_check_type(value, t) for t in expected_types):
            got = type(value).__name__
            want = " or ".join(expected_types)
            return False, (
                f"Parameter '{key}' for tool '{name}' has wrong type: "
                f"expected {want}, got {got}"
            )
    return True, None


__all__ = [
    "TOOL_SEARCH_NAME",
    "TOOL_DESCRIBE_NAME",
    "TOOL_CALL_NAME",
    "BRIDGE_TOOL_NAMES",
    "ToolSearchConfig",
    "CatalogEntry",
    "AssemblyResult",
    "load_config",
    "load_config_readonly",
    "is_deferrable_tool_name",
    "classify_tools",
    "estimate_tokens_from_schemas",
    "should_activate",
    "build_catalog",
    "build_catalog_listing",
    "build_catalog_listing_with_form",
    "listing_token_budget",
    "search_catalog",
    "bridge_tool_schemas",
    "assemble_tool_defs",
    "is_bridge_tool",
    "dispatch_tool_search",
    "dispatch_tool_describe",
    "resolve_underlying_call",
    "scoped_deferrable_names",
    "validate_deferred_call_args",
    "normalize_tool_call_entries",
    "CONNECTOR_BATCH_SENTINEL",
    "is_connector_name",
    "effective_core_tool_names",
    "get_previous_queries",
    "note_tool_search",
    "reset_search_streak",
    "_non_deferrable_error",
    "_CORE_TOOL_ALTERNATIVES",
    "clear_describe_cache",
    "clear_search_catalog_cache",
    "validate_tool_args",
]


# ---- BEGIN PLUGIN-COMPAT (revert-scheduled; see COMPAT_MANIFEST.md) ----
# Names external plugins imported from this module before the Sep 2026 decomposition.
# Internal code MUST NOT use these (scripts/check_compat_pointers.py fails CI if it does).
# The whole block is removed by reverting the commit that added it.
from typing import Literal  # noqa: F401,E402
import copy  # noqa: F401,E402
from dataclasses import field  # noqa: F401,E402
import re  # noqa: F401,E402
import snowballstemmer  # noqa: F401,E402
import threading  # noqa: F401,E402


def build_catalog_listing(
    deferrable: List[Dict[str, Any]],
    *,
    max_tokens: int = 4000,
) -> Tuple[Optional[str], str]:
    return build_catalog_listing_with_form(deferrable, max_tokens=max_tokens)
# ---- END PLUGIN-COMPAT ----
