"""Progressive tool disclosure ("tool search") for Hermes Agent.

When enabled, MCP and non-core plugin tools are replaced in the model-visible
tools array by three bridge tools — ``tool_search``, ``tool_describe``,
``tool_call`` — and surfaced on demand. Core Hermes tools never defer.

Design constraints this module is built around (see ``openclaw-tool-search-report``
for the full rationale):

* Core tools defined in ``toolsets._HERMES_CORE_TOOLS`` are *never* deferred.
  Always-load means always-load. No exceptions.
* The threshold gate runs every assembly: when deferrable tools would consume
  less than ``threshold_pct`` of the model's context window (default 10%),
  tool search is a no-op and the tools array passes through unchanged.
* The catalog is stateless across turns and tools-array assemblies. It is
  rebuilt from the current tool-defs list every time. This is the lesson
  from OpenClaw's cron regression (openclaw/openclaw#84141): a session-keyed
  catalog that drifts out of sync with the live tool registry produces
  silent tool dropouts.
* Bridge tools route through ``model_tools.handle_function_call`` exactly
  like a direct call, so guardrails, plugin pre/post hooks, approval flows,
  and tool-result truncation all fire identically.
* Display and trajectory unwrap is implemented here so the user (CLI activity
  feed, gateway, saved trajectories) always sees the underlying tool, not
  the bridge.
"""

from __future__ import annotations

import json
import logging
import math
import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Tuple

logger = logging.getLogger("tools.tool_search")


# Bridge tool names. These names are reserved and may not collide with a
# user/plugin/MCP tool — registration of any tool with these names is
# rejected by the registry's existing override-protection logic.
TOOL_SEARCH_NAME = "tool_search"
TOOL_DESCRIBE_NAME = "tool_describe"
TOOL_CALL_NAME = "tool_call"

BRIDGE_TOOL_NAMES = frozenset({TOOL_SEARCH_NAME, TOOL_DESCRIBE_NAME, TOOL_CALL_NAME})

# When estimating tokens from char count without a real tokenizer, this is
# the cheap rule of thumb that's stable across providers. Roughly 4 chars
# per token for English+JSON. Underestimating leads to false negatives
# (tool search not activated when it should); overestimating leads to false
# positives (activated when not needed). 4.0 errs slightly toward
# underestimating, which is the safer default.
CHARS_PER_TOKEN = 4.0

# ── #1144 — consecutive-tool_search streak tracking ───────────────────────
# Per-session count of ``tool_search`` calls with no intervening ``tool_call``.
# When the model keeps reformulating queries but never invokes a discovered
# tool, ``dispatch_tool_search`` appends a ``fallback_directive`` once the
# streak crosses ``ToolSearchConfig.search_streak_threshold``. The counter
# resets on any ``tool_call``. Keyed by session_id; a None session_id is not
# tracked (keeps pure-function tests that pass no session_id unaffected).
_SEARCH_STREAK: Dict[str, int] = {}


def note_tool_search(session_id: Optional[str]) -> int:
    """Increment the consecutive-search streak for ``session_id``; return it."""
    if not session_id:
        return 0
    _SEARCH_STREAK[session_id] = _SEARCH_STREAK.get(session_id, 0) + 1
    return _SEARCH_STREAK[session_id]


def reset_search_streak(session_id: Optional[str]) -> None:
    """Reset the streak — call when the model invokes a discovered tool."""
    if session_id and session_id in _SEARCH_STREAK:
        _SEARCH_STREAK[session_id] = 0


def _fallback_directive(streak: int) -> str:
    """The nudge appended to a ``tool_search`` result when the streak is high."""
    return (
        f"You have run tool_search {streak} times in a row without calling a "
        "discovered tool. Try one of: (a) broaden the query (more general terms), "
        "(b) call tool_describe on a likely candidate to confirm it does what you "
        "need, or (c) proceed without the deferred tool if the core tools suffice."
    )


# ---------------------------------------------------------------------------
# Configuration plumbing
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ToolSearchConfig:
    """Resolved, validated tool-search configuration for a single assembly."""

    enabled: str  # "auto" | "on" | "off"
    threshold_pct: float  # 0..100 — only used when enabled == "auto"
    search_default_limit: int
    max_search_limit: int
    # Native (core) toolsets the operator has opted in to progressive
    # disclosure. Empty by default — core tools never defer unless their
    # toolset name appears here. See ``effective_core_tool_names`` for how
    # this subtracts opted-in core tools from the never-defer set.
    defer_core_toolsets: frozenset[str] = frozenset()
    # #1144 — after this many consecutive ``tool_search`` calls with no
    # intervening ``tool_call``, append a fallback directive to the result
    # nudging the model to broaden the query, check tool_describe, or proceed
    # without the deferred tool. 0 disables the guard.
    search_streak_threshold: int = 3

    @classmethod
    def from_raw(cls, raw: Any) -> "ToolSearchConfig":
        """Build a config from a raw dict / bool / None.

        Accepts the legacy bool shape (``tools.tool_search: true``) and the
        dict shape (``tools.tool_search: {enabled: auto, ...}``). Validates
        and clamps every numeric field; unknown values fall back to safe
        defaults rather than raising, so a typo in user config does not
        break the agent.
        """
        if raw is True:
            return cls(enabled="auto", threshold_pct=10.0,
                       search_default_limit=5, max_search_limit=20)
        if raw is False:
            return cls(enabled="off", threshold_pct=10.0,
                       search_default_limit=5, max_search_limit=20)
        if not isinstance(raw, dict):
            return cls(enabled="auto", threshold_pct=10.0,
                       search_default_limit=5, max_search_limit=20)

        enabled_raw = str(raw.get("enabled", "auto")).strip().lower()
        if enabled_raw in ("true", "1", "yes"):
            enabled = "on"
        elif enabled_raw in ("false", "0", "no"):
            enabled = "off"
        elif enabled_raw in ("auto", "on", "off"):
            enabled = enabled_raw
        else:
            enabled = "auto"

        threshold_pct = _safe_float(raw.get("threshold_pct"), 10.0)
        threshold_pct = max(0.0, min(100.0, threshold_pct))

        max_search_limit = max(1, min(50, _safe_int(raw.get("max_search_limit"), 20)))
        search_default_limit = max(1, min(max_search_limit,
                                          _safe_int(raw.get("search_default_limit"), 5)))
        streak_threshold = max(0, min(20, _safe_int(raw.get("search_streak_threshold"), 3)))

        return cls(
            enabled=enabled,
            threshold_pct=threshold_pct,
            search_default_limit=search_default_limit,
            max_search_limit=max_search_limit,
            defer_core_toolsets=_parse_toolset_list(raw.get("defer_core_toolsets")),
            search_streak_threshold=streak_threshold,
        )


def _parse_toolset_list(value: Any) -> frozenset[str]:
    """Coerce a raw config value into a frozenset of toolset names.

    Accepts a list of strings or a single comma-separated string. Non-string
    members and blanks are dropped so a malformed entry can't crash assembly.
    """
    if value is None:
        return frozenset()
    if isinstance(value, str):
        items = value.split(",")
    elif isinstance(value, (list, tuple, set, frozenset)):
        items = list(value)
    else:
        return frozenset()
    names = {str(item).strip() for item in items if isinstance(item, str) and str(item).strip()}
    return frozenset(names)


def _safe_int(value: Any, fallback: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


def _safe_float(value: Any, fallback: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return fallback


def load_config() -> ToolSearchConfig:
    """Load tool-search config from the user config file."""
    try:
        from hermes_cli.config import load_config as _load
        cfg = _load() or {}
        tools_cfg = cfg.get("tools") if isinstance(cfg.get("tools"), dict) else {}
        if not isinstance(tools_cfg, dict):
            tools_cfg = {}
        return ToolSearchConfig.from_raw(tools_cfg.get("tool_search"))
    except Exception as e:
        logger.debug("Failed to load tool-search config: %s", e)
        return ToolSearchConfig.from_raw(None)


# ---------------------------------------------------------------------------
# Tool classification
# ---------------------------------------------------------------------------


def _hermes_core_tools() -> frozenset[str]:
    """Return the raw ``_HERMES_CORE_TOOLS`` set, unfiltered by config.

    Imported lazily because ``toolsets`` imports from ``tools.registry``
    and we don't want a hard cycle.
    """
    try:
        from toolsets import _HERMES_CORE_TOOLS
        return frozenset(_HERMES_CORE_TOOLS)
    except Exception:
        return frozenset()


def _core_tools_in_toolsets(toolset_names: frozenset[str]) -> frozenset[str]:
    """Return the core tools that belong to any of ``toolset_names``.

    A core tool "belongs to" a toolset if the static ``TOOLSETS`` mapping or
    the live registry places it there. Resolved against both so an operator
    can name either a static toolset (e.g. ``image_gen``) or a registry
    toolset. Only names that are actually in ``_HERMES_CORE_TOOLS`` are
    returned — naming a non-core toolset is a no-op (those tools are already
    deferrable by default).
    """
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


def effective_core_tool_names(config: Optional[ToolSearchConfig] = None) -> frozenset[str]:
    """Return the set of tool names that must NEVER be deferred.

    Starts from ``_HERMES_CORE_TOOLS`` and subtracts any core tool whose
    toolset the operator opted in to progressive disclosure via
    ``tools.tool_search.defer_core_toolsets``. This is the single source of
    truth consulted by ``is_deferrable_tool_name``, so assembly-time
    classification and dispatch/scope-time validation always agree — a core
    tool deferred at assembly is callable back via the bridge, and one that
    is not deferred is rejected by the bridge. Mismatch here is exactly the
    OpenClaw silent-dropout class of bug.
    """
    core = _hermes_core_tools()
    if config is None:
        config = load_config()
    opted_in = _core_tools_in_toolsets(config.defer_core_toolsets)
    if not opted_in:
        return core
    return frozenset(core - opted_in)


def is_deferrable_tool_name(name: str, config: Optional[ToolSearchConfig] = None) -> bool:
    """Return True if a tool with this name is *eligible* for deferral.

    A tool is deferrable iff it is registered with an MCP toolset prefix
    OR it is not in the *effective* core set. Core tools are never deferred
    (this protects against accidental shadowing) unless their toolset is
    explicitly opted in via ``defer_core_toolsets``.

    ``config`` is resolved from the user config when omitted so the
    no-config call sites (bridge dispatch, scope validation) stay in sync
    with assembly-time classification.
    """
    if name in BRIDGE_TOOL_NAMES:
        return False
    if config is None:
        config = load_config()
    if name in effective_core_tool_names(config):
        return False
    # An opted-in core tool is a *known* real tool (it is in
    # _HERMES_CORE_TOOLS, just excluded from the effective never-defer set).
    # Treat it as deferrable directly rather than re-resolving it through the
    # registry: that keeps the assembly/dispatch decision invariant under
    # transient registry state (a core tool that is unregistered at the exact
    # moment of a dispatch check would otherwise flip to "not deferrable" and
    # become uncallable through the bridge — a silent dropout).
    if name in _hermes_core_tools():
        return True
    # Check registry toolset for MCP prefix.
    try:
        from tools.registry import registry
        entry = registry.get_entry(name)
        if entry is None:
            return False
        if entry.toolset.startswith("mcp-"):
            return True
        # Non-MCP, non-core → plugin tool, eligible.
        return True
    except Exception:
        return False


def classify_tools(
    tool_defs: List[Dict[str, Any]],
    config: Optional[ToolSearchConfig] = None,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Split a tool-defs list into (visible, deferrable).

    ``visible`` retains every tool that must stay in the model-facing array:
    every effective-core tool, plus any tool we can't classify. ``deferrable``
    is the candidate set for catalog entry. ``config`` is resolved from the
    user config when omitted.
    """
    if config is None:
        config = load_config()
    visible: List[Dict[str, Any]] = []
    deferrable: List[Dict[str, Any]] = []
    for td in tool_defs:
        fn = td.get("function") or {}
        name = fn.get("name", "")
        if name in BRIDGE_TOOL_NAMES:
            # Should never happen — bridge tools are added after classification —
            # but be defensive.
            continue
        if is_deferrable_tool_name(name, config):
            deferrable.append(td)
        else:
            visible.append(td)
    return visible, deferrable


# ---------------------------------------------------------------------------
# Token estimation and threshold gate
# ---------------------------------------------------------------------------


def estimate_tokens_from_schemas(tool_defs: Iterable[Dict[str, Any]]) -> int:
    """Estimate the token cost of a tool-defs list via the chars/4 rule.

    Cheap and stable across providers. The number doesn't need to be exact —
    it gates the activate/skip decision, and a typical 200K context with a
    10% threshold means the decision flips around 20K tokens of schema.
    Order-of-magnitude precision is fine.
    """
    total_chars = 0
    for td in tool_defs:
        try:
            total_chars += len(json.dumps(td, ensure_ascii=False, separators=(",", ":")))
        except (TypeError, ValueError):
            total_chars += len(str(td))
    return int(math.ceil(total_chars / CHARS_PER_TOKEN))


def should_activate(
    config: ToolSearchConfig,
    deferrable_tokens: int,
    context_length: Optional[int],
) -> bool:
    """Decide whether tool search should activate for the current assembly.

    ``"off"`` skips unconditionally. ``"on"`` activates unconditionally
    (as long as there is at least one deferrable tool — there's no point
    swapping a no-op). ``"auto"`` activates when the deferrable schemas
    would consume ``threshold_pct`` of context or more.
    """
    if config.enabled == "off":
        return False
    if deferrable_tokens <= 0:
        return False
    if config.enabled == "on":
        return True
    # auto
    if not context_length or context_length <= 0:
        # Without a known context size, fall back to a fixed 20K-token cutoff
        # — the cliff above which Anthropic and OpenAI both saw quality drops.
        return deferrable_tokens >= 20_000
    threshold_tokens = int(context_length * (config.threshold_pct / 100.0))
    return deferrable_tokens >= threshold_tokens


# ---------------------------------------------------------------------------
# Catalog + BM25 retrieval
# ---------------------------------------------------------------------------


@dataclass
class CatalogEntry:
    """One deferrable tool, in a form the bridge tools can search and serve."""

    name: str
    description: str
    schema: Dict[str, Any]  # The full {"type":"function", "function": {...}} entry.
    source: str  # "mcp" | "plugin" | "other"
    source_name: str  # Toolset name, e.g. "mcp-github" or "kanban"

    # Pre-tokenized fields for BM25.
    _tokens: List[str] = field(default_factory=list)


_TOKEN_RE = re.compile(r"[A-Za-z0-9]+")


def _tokenize(text: str) -> List[str]:
    if not text:
        return []
    return [t.lower() for t in _TOKEN_RE.findall(text)]


def _entry_search_text(td: Dict[str, Any]) -> str:
    """Build the search-text blob for a deferrable tool.

    Includes the tool name (with underscores broken into words so BM25 can
    match against query terms), the description, and the names of the
    top-level parameters. Schema bodies are deliberately excluded —
    indexing them adds noise without improving recall in our measurement.
    """
    fn = td.get("function") or {}
    name = fn.get("name", "")
    desc = fn.get("description", "") or ""
    params = ((fn.get("parameters") or {}).get("properties") or {})
    param_names = " ".join(params.keys())
    # Break snake_case and dotted names into words for BM25.
    name_words = name.replace("_", " ").replace(".", " ").replace("-", " ").replace(":", " ")
    return f"{name_words} {desc} {param_names}"


def _classify_source(name: str) -> Tuple[str, str]:
    """Return (source_kind, source_name) for a registered tool name."""
    try:
        from tools.registry import registry
        entry = registry.get_entry(name)
        if entry is None:
            return ("other", "")
        if entry.toolset.startswith("mcp-"):
            return ("mcp", entry.toolset)
        return ("plugin", entry.toolset)
    except Exception:
        return ("other", "")


def build_catalog(tool_defs: List[Dict[str, Any]]) -> List[CatalogEntry]:
    """Build the deferred-tool catalog from a tool-defs list.

    Caller is expected to pass only the deferrable subset (``classify_tools``
    returns it as the second element).
    """
    catalog: List[CatalogEntry] = []
    for td in tool_defs:
        fn = td.get("function") or {}
        name = fn.get("name", "")
        if not name:
            continue
        desc = fn.get("description", "") or ""
        source, source_name = _classify_source(name)
        entry = CatalogEntry(
            name=name,
            description=desc,
            schema=td,
            source=source,
            source_name=source_name,
            _tokens=_tokenize(_entry_search_text(td)),
        )
        catalog.append(entry)
    return catalog


def _bm25_score(query_tokens: List[str], doc_tokens: List[str],
                doc_lengths: List[int], avg_dl: float,
                doc_freq: Dict[str, int], n_docs: int,
                k1: float = 1.5, b: float = 0.75) -> float:
    """Standard BM25 score for one query against one document.

    Inlined small implementation rather than adding a dependency. Performance
    is fine — the catalog is bounded by N (tools) typically < 500, and we
    score against the in-memory tokens list.
    """
    if not doc_tokens:
        return 0.0
    score = 0.0
    dl = len(doc_tokens)
    # Pre-count tokens in the doc.
    doc_tf: Dict[str, int] = {}
    for t in doc_tokens:
        doc_tf[t] = doc_tf.get(t, 0) + 1
    for q in query_tokens:
        df = doc_freq.get(q, 0)
        if df == 0:
            continue
        idf = math.log(1 + (n_docs - df + 0.5) / (df + 0.5))
        tf = doc_tf.get(q, 0)
        if tf == 0:
            continue
        norm = tf * (k1 + 1) / (tf + k1 * (1 - b + b * dl / max(avg_dl, 1.0)))
        score += idf * norm
    return score


def search_catalog(catalog: List[CatalogEntry], query: str, limit: int = 5) -> List[CatalogEntry]:
    """Return the top-``limit`` catalog entries for ``query`` by BM25.

    Falls back to a stable name-substring match when BM25 yields no hits
    above zero. That ensures a query like ``"github"`` against a catalog
    where every tool is named ``github_*`` still returns results — BM25
    can underperform when query and document share only one token that
    appears in every document (zero IDF).
    """
    if not catalog or limit <= 0:
        return []
    query_tokens = _tokenize(query)
    if not query_tokens:
        return []

    # Precompute doc statistics.
    doc_lengths = [len(e._tokens) for e in catalog]
    avg_dl = sum(doc_lengths) / max(len(doc_lengths), 1)
    doc_freq: Dict[str, int] = {}
    for e in catalog:
        seen = set(e._tokens)
        for t in seen:
            doc_freq[t] = doc_freq.get(t, 0) + 1
    n_docs = len(catalog)

    scored: List[Tuple[float, CatalogEntry]] = []
    for entry in catalog:
        s = _bm25_score(query_tokens, entry._tokens, doc_lengths, avg_dl,
                        doc_freq, n_docs)
        if s > 0:
            scored.append((s, entry))

    if not scored:
        # Substring fallback against the original tool name.
        ql = query.lower()
        for entry in catalog:
            if ql in entry.name.lower():
                scored.append((0.1, entry))

    scored.sort(key=lambda x: x[0], reverse=True)
    return [e for _, e in scored[:limit]]


# ---------------------------------------------------------------------------
# Bridge tool schemas
# ---------------------------------------------------------------------------


def bridge_tool_schemas(deferred_count: int) -> List[Dict[str, Any]]:
    """Build the bridge tool schemas to inject in place of deferred tools.

    The schemas are intentionally short — every byte added here is a byte
    the user pays on every turn. Descriptions are tuned to be unambiguous
    about the call sequence the model should follow.
    """
    desc_search = (
        f"Search {deferred_count} additional tools that are loaded on demand. "
        "Returns up to ``limit`` matches with name and description. Follow "
        f"with `{TOOL_DESCRIBE_NAME}` to load a tool's full parameter schema, "
        f"then `{TOOL_CALL_NAME}` to invoke it. Tools listed at the top of this "
        "system prompt are already available and do not need to be searched."
    )
    desc_describe = (
        f"Load the full JSON schema for one tool returned by `{TOOL_SEARCH_NAME}`. "
        f"Required before `{TOOL_CALL_NAME}` if the tool's parameters are unknown."
    )
    desc_call = (
        "Invoke a deferred tool by name with the given arguments. Argument shape "
        f"matches the tool's schema (see `{TOOL_DESCRIBE_NAME}`). Policy, hooks, "
        "and approvals run exactly as for any directly-listed tool."
    )

    return [
        {
            "type": "function",
            "function": {
                "name": TOOL_SEARCH_NAME,
                "description": desc_search,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "Keywords describing the capability you need (e.g. 'create github issue').",
                        },
                        "limit": {
                            "type": "integer",
                            "description": "Maximum number of results to return. Default 5.",
                        },
                    },
                    "required": ["query"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": TOOL_DESCRIBE_NAME,
                "description": desc_describe,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "name": {
                            "type": "string",
                            "description": "Exact tool name (as returned by tool_search).",
                        },
                    },
                    "required": ["name"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": TOOL_CALL_NAME,
                "description": desc_call,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "name": {
                            "type": "string",
                            "description": "Exact tool name to invoke.",
                        },
                        "arguments": {
                            "type": "object",
                            "description": "Arguments for the tool, matching its schema.",
                        },
                    },
                    "required": ["name", "arguments"],
                },
            },
        },
    ]


# ---------------------------------------------------------------------------
# Public entry point: assemble tool-defs with optional tool search
# ---------------------------------------------------------------------------


@dataclass
class AssemblyResult:
    """Outcome of one assembly. Useful for tests and observability."""

    tool_defs: List[Dict[str, Any]]
    activated: bool
    deferred_count: int = 0
    deferred_tokens: int = 0
    threshold_tokens: int = 0


def assemble_tool_defs(
    tool_defs: List[Dict[str, Any]],
    *,
    context_length: Optional[int] = None,
    config: Optional[ToolSearchConfig] = None,
) -> AssemblyResult:
    """Return the tool-defs list the model should actually see.

    When tool search is inactive (off, no deferrable tools, or below
    threshold), this is a passthrough. When active, MCP and plugin tools
    are stripped from the visible list and replaced with the three bridge
    tools. Core tools are *never* deferred regardless of config.

    Idempotent: calling with bridge tools already in the input is a no-op
    (they classify as non-core/non-deferrable but their names are reserved,
    so they are filtered out of the deferrable set).
    """
    if config is None:
        config = load_config()

    # Defensive: strip any bridge tools that may already be in the list
    # (e.g. someone called assemble twice).
    incoming = [td for td in tool_defs
                if (td.get("function") or {}).get("name") not in BRIDGE_TOOL_NAMES]

    visible, deferrable = classify_tools(incoming, config)
    if not deferrable:
        return AssemblyResult(tool_defs=incoming, activated=False)

    deferrable_tokens = estimate_tokens_from_schemas(deferrable)
    if not should_activate(config, deferrable_tokens, context_length):
        return AssemblyResult(
            tool_defs=incoming,
            activated=False,
            deferred_count=len(deferrable),
            deferred_tokens=deferrable_tokens,
            threshold_tokens=int((context_length or 0) * (config.threshold_pct / 100.0)),
        )

    bridge = bridge_tool_schemas(len(deferrable))
    result = visible + bridge
    threshold_tokens = int((context_length or 0) * (config.threshold_pct / 100.0))

    logger.info(
        "tool_search activated: %d core/visible tools kept, %d deferred (~%d tokens, threshold ~%d)",
        len(visible), len(deferrable), deferrable_tokens, threshold_tokens,
    )

    return AssemblyResult(
        tool_defs=result,
        activated=True,
        deferred_count=len(deferrable),
        deferred_tokens=deferrable_tokens,
        threshold_tokens=threshold_tokens,
    )


# ---------------------------------------------------------------------------
# Bridge tool dispatch
# ---------------------------------------------------------------------------


def is_bridge_tool(name: str) -> bool:
    return name in BRIDGE_TOOL_NAMES


# #1015 — cache for tool_describe results. Keyed by (name, toolset_signature)
# so the cache invalidates naturally when the tool set changes (different
# session, enabled/disabled toolsets). The value is the JSON string returned
# by dispatch_tool_describe, so a cache hit skips the full catalog scan.
_describe_cache: dict[tuple[str, str], str] = {}
_DESCRIBE_CACHE_MAX = 64


def _toolset_signature(tool_defs: List[Dict[str, Any]]) -> str:
    """A stable signature of the current tool definitions for cache keying."""
    names = sorted(
        (td.get("function") or {}).get("name", "")
        for td in tool_defs
        if (td.get("function") or {}).get("name")
    )
    return "|".join(names)


def _format_search_hit(entry: CatalogEntry) -> Dict[str, Any]:
    return {
        "name": entry.name,
        "source": entry.source,
        "source_name": entry.source_name,
        # Cap description so a chatty MCP server doesn't blow up the result.
        "description": (entry.description or "")[:400],
    }


def dispatch_tool_search(args: Dict[str, Any],
                         *,
                         current_tool_defs: List[Dict[str, Any]],
                         config: Optional[ToolSearchConfig] = None,
                         session_id: Optional[str] = None) -> str:
    """Execute the ``tool_search`` bridge tool. Returns a JSON string."""
    if config is None:
        config = load_config()
    query = str(args.get("query") or "").strip()
    if not query:
        return json.dumps({"error": "query is required"}, ensure_ascii=False)

    raw_limit = args.get("limit")
    if raw_limit is None:
        limit = config.search_default_limit
    else:
        limit = max(1, min(config.max_search_limit, _safe_int(raw_limit, config.search_default_limit)))

    _, deferrable = classify_tools(current_tool_defs, config)
    catalog = build_catalog(deferrable)
    hits = search_catalog(catalog, query, limit=limit)
    result: Dict[str, Any] = {
        "query": query,
        "total_available": len(catalog),
        "matches": [_format_search_hit(h) for h in hits],
    }
    # #1144 — nudge the model after N consecutive searches with no tool_call.
    threshold = config.search_streak_threshold
    if threshold and threshold > 0:
        streak = note_tool_search(session_id)
        if streak >= threshold:
            result["fallback_directive"] = _fallback_directive(streak)
    return json.dumps(result, ensure_ascii=False)


def _fuzzy_tool_names(query: str, available: List[str], limit: int = 3) -> List[str]:
    """Return up to ``limit`` tool names closest to ``query`` by substring /
    edit-distance. Used so ``tool_describe`` can suggest the right name when
    the model's requested name is slightly wrong (#978), avoiding a separate
    ``tool_search`` round-trip."""
    q = query.lower()
    if not q or not available:
        return []
    # Fast path: substring match (catches typos like "github_create" →
    # "github_create_issue").
    sub = [n for n in available if q in n.lower()]
    if sub:
        return sorted(sub, key=len)[:limit]

    # Edit-distance fallback for near-misses.
    def _dist(a: str, b: str) -> int:
        """Simple Levenshtein distance (small strings, no dep needed)."""
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
    # Only suggest if reasonably close (distance ≤ 3 for short names).
    return [n for n in scored if _dist(q, n) <= max(3, len(q) // 3)]


def dispatch_tool_describe(args: Dict[str, Any],
                           *,
                           current_tool_defs: List[Dict[str, Any]],
                           config: Optional[ToolSearchConfig] = None) -> str:
    """Execute the ``tool_describe`` bridge tool. Returns a JSON string."""
    if config is None:
        config = load_config()
    name = str(args.get("name") or "").strip()
    if not name:
        return json.dumps({"error": "name is required"}, ensure_ascii=False)

    # #1015 — check the describe cache first. Repeated calls for the same
    # tool name (common when the model forgets the schema between turns)
    # hit the cache and skip the full catalog scan, eliminating the
    # re-classification overhead that was a top failure source.
    sig = _toolset_signature(current_tool_defs)
    cache_key = (name, sig)
    cached = _describe_cache.get(cache_key)
    if cached is not None:
        return cached

    result = _dispatch_tool_describe_inner(args, name, current_tool_defs, config)
    # Cache successful results (not error responses — those may change as
    # tools are added/removed).
    if '"error"' not in result:
        if len(_describe_cache) >= _DESCRIBE_CACHE_MAX:
            # Evict oldest entries (dict preserves insertion order in 3.7+).
            _oldest_key = next(iter(_describe_cache))
            del _describe_cache[_oldest_key]
        _describe_cache[cache_key] = result
    return result


def _dispatch_tool_describe_inner(
    args: Dict[str, Any],
    name: str,
    current_tool_defs: List[Dict[str, Any]],
    config: "ToolSearchConfig",
) -> str:
    """Inner logic for dispatch_tool_describe, separated for caching."""
    if not is_deferrable_tool_name(name, config):
        # #978 — fuzzy name matching even for non-deferrable names: the
        # model may have slightly misspelled a deferrable tool. Suggest
        # close matches from the current tool defs so it can self-correct
        # without a separate tool_search round-trip.
        _, deferrable = classify_tools(current_tool_defs, config)
        available_names = [
            (td.get("function") or {}).get("name", "")
            for td in deferrable
        ]
        suggestions = _fuzzy_tool_names(name, available_names)
        if suggestions:
            return json.dumps({
                "error": (
                    f"'{name}' is not a deferrable tool. Did you mean one of: "
                    f"{', '.join(suggestions)}? Use the exact name with "
                    f"tool_describe or tool_call."
                ),
                "suggestions": suggestions,
            }, ensure_ascii=False)
        return json.dumps({
            "error": (
                f"'{name}' is not a deferrable tool. If you see it in the tools list "
                "already, call it directly; otherwise check the spelling against tool_search."
            ),
        }, ensure_ascii=False)
    _, deferrable = classify_tools(current_tool_defs, config)
    for td in deferrable:
        fn = td.get("function") or {}
        if fn.get("name") == name:
            return json.dumps({
                "name": name,
                "description": fn.get("description", ""),
                "parameters": fn.get("parameters", {}),
            }, ensure_ascii=False)
    # #978 — fuzzy name matching: suggest closest matches so the agent can
    # self-correct without a separate tool_search round-trip.
    available_names = [
        (td.get("function") or {}).get("name", "")
        for td in deferrable
    ]
    suggestions = _fuzzy_tool_names(name, available_names)
    if suggestions:
        return json.dumps({
            "error": (
                f"'{name}' is not currently available. Did you mean one of: "
                f"{', '.join(suggestions)}? Use the exact name with tool_describe "
                f"or tool_call."
            ),
            "suggestions": suggestions,
        }, ensure_ascii=False)
    return json.dumps({
        "error": f"'{name}' is not currently available. Re-run tool_search to refresh.",
    }, ensure_ascii=False)


def scoped_deferrable_names(
    tool_defs: List[Dict[str, Any]],
    config: Optional[ToolSearchConfig] = None,
) -> frozenset[str]:
    """Return the set of deferrable tool names present in ``tool_defs``.

    ``tool_defs`` is expected to be the *pre-assembly* tool list for the
    current session's toolset scope (i.e. what
    ``get_tool_definitions(skip_tool_search_assembly=True)`` returns for the
    session's enabled/disabled toolsets). The resulting set is the universe of
    tools the session may legitimately reach through ``tool_call``. Used as a
    scoping gate by both the ``model_tools`` bridge dispatch and the
    ``tool_executor`` unwrap so a restricted-toolset session can never invoke
    an out-of-scope tool via the bridge.

    ``config`` is resolved from the user config when omitted so the scope gate
    sees the same deferred set as assembly (including any opted-in core
    toolsets).
    """
    if config is None:
        config = load_config()
    names: set[str] = set()
    for td in tool_defs:
        name = (td.get("function") or {}).get("name", "")
        if name and is_deferrable_tool_name(name, config):
            names.add(name)
    return frozenset(names)


# Map JSON Schema type strings to Python types for validation. ``number``
# accepts both int and float (JSON ints are a subset of floats).
_SCHEMA_PY_TYPES: Dict[str, Tuple[type, ...]] = {
    "string": (str,),
    "integer": (int,),
    "number": (int, float),
    "boolean": (bool,),
    "array": (list, tuple),
    "object": (dict,),
}


def validate_tool_args(
    name: str,
    args: Dict[str, Any],
    schema: Optional[dict],
) -> Tuple[bool, Optional[str]]:
    """Validate *args* against a tool's OpenAI-format parameter *schema*.

    Returns ``(True, None)`` when valid, ``(False, error_message)`` otherwise.
    Checks required-parameter presence and basic type matching for the
    common JSON Schema types. Only top-level parameters are validated.
    """
    if not schema:
        return True, None
    params = schema.get("parameters") or {}
    properties = params.get("properties") or {}
    required = params.get("required") or []

    if not isinstance(args, dict):
        return False, f"Arguments for '{name}' must be an object"

    # Required parameters
    for req in required:
        if req not in args or args[req] is None:
            return False, f"Missing required parameter '{req}' for tool '{name}'"

    # Type matching
    for key, value in args.items():
        if value is None:
            continue  # null is acceptable for optional params
        prop = properties.get(key)
        if not prop:
            continue  # unknown params are not our concern here
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


def _check_type(value: Any, type_str: str) -> bool:
    """Check whether *value* matches the JSON Schema *type_str*."""
    if type_str == "integer":
        # bool is a subclass of int in Python; reject it for integer params.
        return isinstance(value, int) and not isinstance(value, bool)
    py_types = _SCHEMA_PY_TYPES.get(type_str)
    if py_types is None:
        return True  # unknown type — don't block dispatch
    return isinstance(value, py_types)


def resolve_underlying_call(
    args: Dict[str, Any],
    config: Optional[ToolSearchConfig] = None,
) -> Tuple[Optional[str], Dict[str, Any], Optional[str]]:
    """Parse a ``tool_call`` invocation into (underlying_name, args, error_msg).

    Used by:
    * the dispatcher in ``model_tools.handle_function_call``,
    * the display layer (so the activity feed shows the underlying tool),
    * the trajectory recorder.

    ``config`` is resolved from the user config when omitted so the
    deferrability check matches assembly-time classification.

    On parse error, returns ``(None, {}, error_message)``.
    """
    if config is None:
        config = load_config()
    name = str(args.get("name") or "").strip()
    if not name:
        return None, {}, "tool_call requires a 'name' argument"
    if name in BRIDGE_TOOL_NAMES:
        return None, {}, f"tool_call cannot invoke '{name}' (it is itself a bridge tool)"
    raw_args = args.get("arguments")
    if raw_args is None:
        raw_args = {}
    if isinstance(raw_args, str):
        try:
            raw_args = json.loads(raw_args)
        except json.JSONDecodeError as e:
            return None, {}, f"tool_call 'arguments' is not valid JSON: {e}"
    if not isinstance(raw_args, dict):
        return None, {}, "tool_call 'arguments' must be an object"
    if not is_deferrable_tool_name(name, config):
        return None, {}, (
            f"'{name}' is not a deferrable tool. If it appears in the model-facing tools "
            "list already, call it directly instead of via tool_call."
        )
    return name, raw_args, None


def clear_describe_cache() -> None:
    """Clear the tool_describe result cache (#1015)."""
    _describe_cache.clear()


__all__ = [
    "TOOL_SEARCH_NAME",
    "TOOL_DESCRIBE_NAME",
    "TOOL_CALL_NAME",
    "BRIDGE_TOOL_NAMES",
    "ToolSearchConfig",
    "CatalogEntry",
    "AssemblyResult",
    "load_config",
    "effective_core_tool_names",
    "is_deferrable_tool_name",
    "classify_tools",
    "estimate_tokens_from_schemas",
    "should_activate",
    "build_catalog",
    "search_catalog",
    "bridge_tool_schemas",
    "assemble_tool_defs",
    "is_bridge_tool",
    "dispatch_tool_describe",
    "dispatch_tool_search",
    "resolve_underlying_call",
    "validate_tool_args",
    "scoped_deferrable_names",
    "clear_describe_cache",
]
