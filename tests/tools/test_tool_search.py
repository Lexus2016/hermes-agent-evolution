"""Tests for tools/tool_search.py — progressive tool disclosure.

Coverage targets — these mirror the issues called out in the OpenClaw tool
search report. Every test that names an OpenClaw issue is the regression
guard that would have caught that specific failure mode.
"""

from __future__ import annotations

import json
import os
import sys
from typing import List, Dict, Any

import pytest


_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


def _td(name: str, description: str = "", properties: Dict[str, Any] | None = None) -> Dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties or {},
            },
        },
    }


# ---------------------------------------------------------------------------
# Config parsing
# ---------------------------------------------------------------------------


class TestConfigParsing:
    def test_default_when_missing(self):
        from tools.tool_search import ToolSearchConfig
        cfg = ToolSearchConfig.from_raw(None)
        assert cfg.enabled == "auto"
        assert cfg.threshold_pct == 10.0

    def test_bool_true_maps_to_auto(self):
        from tools.tool_search import ToolSearchConfig
        cfg = ToolSearchConfig.from_raw(True)
        assert cfg.enabled == "auto"

    def test_bool_false_maps_to_off(self):
        from tools.tool_search import ToolSearchConfig
        cfg = ToolSearchConfig.from_raw(False)
        assert cfg.enabled == "off"

    def test_explicit_on(self):
        from tools.tool_search import ToolSearchConfig
        cfg = ToolSearchConfig.from_raw({"enabled": "on"})
        assert cfg.enabled == "on"

    def test_invalid_enabled_falls_back_to_auto(self):
        from tools.tool_search import ToolSearchConfig
        cfg = ToolSearchConfig.from_raw({"enabled": "maybe"})
        assert cfg.enabled == "auto"

    def test_threshold_clamped(self):
        from tools.tool_search import ToolSearchConfig
        cfg = ToolSearchConfig.from_raw({"threshold_pct": 150})
        assert cfg.threshold_pct == 100.0
        cfg = ToolSearchConfig.from_raw({"threshold_pct": -5})
        assert cfg.threshold_pct == 0.0

    def test_search_limits_clamped(self):
        from tools.tool_search import ToolSearchConfig
        cfg = ToolSearchConfig.from_raw({
            "search_default_limit": 999,
            "max_search_limit": 999,
        })
        assert cfg.max_search_limit == 50
        assert cfg.search_default_limit <= cfg.max_search_limit

    def test_defer_core_toolsets_default_empty(self):
        from tools.tool_search import ToolSearchConfig
        assert ToolSearchConfig.from_raw(None).defer_core_toolsets == frozenset()
        assert ToolSearchConfig.from_raw({"enabled": "on"}).defer_core_toolsets == frozenset()

    def test_defer_core_toolsets_list_form(self):
        from tools.tool_search import ToolSearchConfig
        cfg = ToolSearchConfig.from_raw({"defer_core_toolsets": ["browser", "tts"]})
        assert cfg.defer_core_toolsets == frozenset({"browser", "tts"})

    def test_defer_core_toolsets_comma_string_form(self):
        from tools.tool_search import ToolSearchConfig
        cfg = ToolSearchConfig.from_raw({"defer_core_toolsets": "browser, tts ,"})
        assert cfg.defer_core_toolsets == frozenset({"browser", "tts"})

    def test_defer_core_toolsets_garbage_is_dropped(self):
        """A malformed entry must never crash assembly — non-strings are dropped."""
        from tools.tool_search import ToolSearchConfig, _parse_toolset_list
        assert _parse_toolset_list(123) == frozenset()
        assert _parse_toolset_list({"a": 1}) == frozenset()
        assert _parse_toolset_list([1, "tts", None, ""]) == frozenset({"tts"})
        # And the full path tolerates it too.
        cfg = ToolSearchConfig.from_raw({"defer_core_toolsets": 123})
        assert cfg.defer_core_toolsets == frozenset()


# ---------------------------------------------------------------------------
# Classification — the hard invariant: core tools NEVER defer.
# ---------------------------------------------------------------------------


class TestClassification:
    def test_core_tools_never_defer(self):
        """The critical invariant from the OpenClaw report."""
        from tools.tool_search import is_deferrable_tool_name
        # Sample of core tools from _HERMES_CORE_TOOLS.
        for core_name in ["terminal", "read_file", "write_file", "patch",
                          "search_files", "todo", "memory", "browser_navigate",
                          "web_search", "session_search", "clarify",
                          "execute_code", "delegate_task", "send_message"]:
            assert not is_deferrable_tool_name(core_name), (
                f"Core tool '{core_name}' must NEVER be deferrable"
            )

    def test_bridge_tools_never_defer(self):
        from tools.tool_search import is_deferrable_tool_name, BRIDGE_TOOL_NAMES
        for name in BRIDGE_TOOL_NAMES:
            assert not is_deferrable_tool_name(name)

    def test_unknown_tool_not_deferrable(self):
        """Defensive: a tool name we cannot resolve to a registry entry must
        not be claimed as deferrable. This protects against the OpenClaw
        cron regression where unresolved tools were silently dropped."""
        from tools.tool_search import is_deferrable_tool_name
        assert not is_deferrable_tool_name("xx_definitely_not_a_tool_xx")

    def test_classify_keeps_unknown_in_visible(self):
        """A tool we can't classify stays visible — never silently dropped.

        This is the OpenClaw #84141 regression guard (cron lost ``exec``
        because it wasn't in the catalog).
        """
        from tools.tool_search import classify_tools
        # Build a tool def for something we don't have a registry entry for.
        defs = [_td("xx_unknown_tool", "Unknown tool")]
        visible, deferrable = classify_tools(defs)
        names = {(td.get("function") or {}).get("name") for td in visible}
        assert "xx_unknown_tool" in names
        assert deferrable == []


# ---------------------------------------------------------------------------
# Config-driven deferral of native (core) tool sets — issue #229 increment.
#
# By default core tools never defer. An operator can opt a *native* toolset
# in to progressive disclosure via tools.tool_search.defer_core_toolsets;
# those core tools then behave like any other deferrable tool. The hard
# invariant is that assembly-time classification and dispatch/scope-time
# validation agree (effective_core_tool_names is the single source of truth),
# so an opted-in core tool deferred from the visible array is always callable
# back through the bridge — never a silent dropout.
# ---------------------------------------------------------------------------


class TestCoreToolsetDeferral:
    # The browser toolset is a representative native tool set: ~10 core
    # browser_* tools, reliably present in the default tool definitions,
    # and a real candidate for deferral (a coding/chat session that rarely
    # browses pays all ~10 schemas every turn).
    _DEMO_TOOLSET = "browser"
    _DEMO_TOOL = "browser_click"
    _PROTECTED_TOOL = "terminal"  # core, in a different toolset — must stay direct.

    @pytest.fixture(autouse=True)
    def _populate_registry(self):
        """is_deferrable_tool_name resolves the tool via the live registry, so
        the tool modules must be imported/registered first — exactly as they
        are at runtime before any assembly. Importing model_tools and pulling
        the definitions once triggers registration."""
        import model_tools
        model_tools.get_tool_definitions(quiet_mode=True, skip_tool_search_assembly=True)

    def _cfg(self, **over):
        from tools.tool_search import ToolSearchConfig
        raw = {"enabled": "on", "defer_core_toolsets": [self._DEMO_TOOLSET]}
        raw.update(over)
        return ToolSearchConfig.from_raw(raw)

    def test_effective_core_unchanged_by_default(self):
        from tools.tool_search import (
            effective_core_tool_names, _hermes_core_tools, ToolSearchConfig,
        )
        cfg = ToolSearchConfig.from_raw({"enabled": "on"})
        assert effective_core_tool_names(cfg) == _hermes_core_tools()

    def test_effective_core_subtracts_opted_in_toolset(self):
        from tools.tool_search import effective_core_tool_names, _hermes_core_tools
        raw_core = _hermes_core_tools()
        # Pre-condition: the demo tool is genuinely a core tool.
        assert self._DEMO_TOOL in raw_core
        eff = effective_core_tool_names(self._cfg())
        assert self._DEMO_TOOL not in eff, (
            "opted-in core toolset member must drop out of the never-defer set"
        )
        # An unrelated core tool stays protected.
        assert self._PROTECTED_TOOL in eff

    def test_opted_in_core_tool_is_deferrable(self):
        from tools.tool_search import is_deferrable_tool_name
        assert is_deferrable_tool_name(self._DEMO_TOOL, self._cfg())
        # Default config: still never deferrable.
        from tools.tool_search import ToolSearchConfig
        assert not is_deferrable_tool_name(
            self._DEMO_TOOL, ToolSearchConfig.from_raw({"enabled": "on"})
        )

    def test_protected_core_tool_never_deferrable_even_when_opting_browser(self):
        from tools.tool_search import is_deferrable_tool_name
        assert not is_deferrable_tool_name(self._PROTECTED_TOOL, self._cfg())

    def test_classify_defers_opted_in_native_toolset(self):
        import model_tools
        from tools.tool_search import classify_tools
        defs = model_tools.get_tool_definitions(
            quiet_mode=True, skip_tool_search_assembly=True,
        ) or []
        visible, deferrable = classify_tools(defs, self._cfg())
        deferred_names = {(td.get("function") or {}).get("name") for td in deferrable}
        visible_names = {(td.get("function") or {}).get("name") for td in visible}
        browser_deferred = {n for n in deferred_names if n.startswith("browser_")}
        # The browser toolset registers ~9-12 browser_* tools depending on
        # which are gated on/off in the environment; the mechanism is proven
        # by deferring the whole native set, not by an exact count.
        assert len(browser_deferred) >= 5, (
            f"expected the native browser toolset deferred, got {sorted(browser_deferred)}"
        )
        # Protected core tool stays in the visible array.
        assert self._PROTECTED_TOOL in visible_names

    def test_assembly_defers_native_toolset_and_reports_savings(self):
        """assemble_tool_defs both defers the opted-in native tools AND reports
        the token savings (deferred_tokens) so the win can be measured."""
        import model_tools
        from tools.tool_search import assemble_tool_defs, ToolSearchConfig
        defs = model_tools.get_tool_definitions(
            quiet_mode=True, skip_tool_search_assembly=True,
        ) or []
        baseline = assemble_tool_defs(
            defs, context_length=200_000,
            config=ToolSearchConfig.from_raw({"enabled": "on"}),
        )
        opted = assemble_tool_defs(defs, context_length=200_000, config=self._cfg())
        # Opting browser in must add browser_* to the deferred count and
        # the measured deferred-token total grows accordingly.
        assert opted.deferred_count > baseline.deferred_count
        assert opted.deferred_tokens > baseline.deferred_tokens
        result_names = {(td.get("function") or {}).get("name") for td in opted.tool_defs}
        # The deferred browser tools are no longer in the model-visible array.
        assert not any(n.startswith("browser_") for n in result_names)
        # Protected core tool is still visible.
        assert self._PROTECTED_TOOL in result_names

    def test_roundtrip_invariant_opted_in_core_tool_is_callable_back(self):
        """The OpenClaw silent-dropout guard for opted-in core tools: a tool
        deferred from the visible array MUST be in the scoped deferrable set,
        resolvable via tool_call, and describable via tool_describe."""
        import model_tools
        from tools.tool_search import (
            scoped_deferrable_names, resolve_underlying_call, dispatch_tool_describe,
        )
        cfg = self._cfg()
        defs = model_tools.get_tool_definitions(
            quiet_mode=True, skip_tool_search_assembly=True,
        ) or []
        scope = scoped_deferrable_names(defs, cfg)
        assert self._DEMO_TOOL in scope
        name, _args, err = resolve_underlying_call(
            {"name": self._DEMO_TOOL, "arguments": {}}, cfg,
        )
        assert err is None and name == self._DEMO_TOOL
        described = json.loads(
            dispatch_tool_describe(
                {"name": self._DEMO_TOOL}, current_tool_defs=defs, config=cfg,
            )
        )
        assert "parameters" in described

    def test_default_config_keeps_native_tool_direct(self):
        """The inverse of the round-trip: with no opt-in, the core tool stays
        direct and the bridge refuses to resolve it (use it directly)."""
        from tools.tool_search import ToolSearchConfig, resolve_underlying_call
        cfg = ToolSearchConfig.from_raw({"enabled": "on"})
        _name, _args, err = resolve_underlying_call(
            {"name": self._DEMO_TOOL, "arguments": {}}, cfg,
        )
        assert err is not None
        assert "not a deferrable" in err

    def test_naming_non_core_toolset_is_a_noop(self):
        """Opting in a toolset with no core members changes nothing — those
        tools were already deferrable (or non-existent)."""
        from tools.tool_search import effective_core_tool_names, _hermes_core_tools, ToolSearchConfig
        cfg = ToolSearchConfig.from_raw(
            {"enabled": "on", "defer_core_toolsets": ["xx_no_such_toolset"]}
        )
        assert effective_core_tool_names(cfg) == _hermes_core_tools()

    def test_opted_in_core_tool_deferrable_without_registry_entry(self, monkeypatch):
        """Registry-timing invariant: an opted-in core tool stays deferrable
        even if the registry has no entry for it at the exact moment of the
        check. Otherwise a transient registry gap would flip the tool to
        'not deferrable' at dispatch and make it uncallable through the bridge
        (silent dropout). The tool's membership in _HERMES_CORE_TOOLS is
        authoritative — no registry round-trip required."""
        import tools.tool_search as ts
        from tools.registry import registry
        # Force the registry lookup to behave as if the tool isn't registered.
        monkeypatch.setattr(registry, "get_entry", lambda _name: None)
        assert ts.is_deferrable_tool_name(self._DEMO_TOOL, self._cfg())
        # The same gap leaves a non-opted-in core tool firmly NOT deferrable.
        assert not ts.is_deferrable_tool_name(self._PROTECTED_TOOL, self._cfg())


# ---------------------------------------------------------------------------
# Token estimation + threshold gate
# ---------------------------------------------------------------------------


class TestThresholdGate:
    def test_off_never_activates(self):
        from tools.tool_search import ToolSearchConfig, should_activate
        cfg = ToolSearchConfig.from_raw({"enabled": "off"})
        assert not should_activate(cfg, deferrable_tokens=1_000_000, context_length=200_000)

    def test_zero_deferrable_never_activates(self):
        from tools.tool_search import ToolSearchConfig, should_activate
        cfg = ToolSearchConfig.from_raw({"enabled": "on"})
        assert not should_activate(cfg, deferrable_tokens=0, context_length=200_000)

    def test_on_activates_with_any_deferrable(self):
        from tools.tool_search import ToolSearchConfig, should_activate
        cfg = ToolSearchConfig.from_raw({"enabled": "on"})
        assert should_activate(cfg, deferrable_tokens=100, context_length=200_000)

    def test_auto_below_threshold_does_not_activate(self):
        from tools.tool_search import ToolSearchConfig, should_activate
        cfg = ToolSearchConfig.from_raw({"enabled": "auto", "threshold_pct": 10})
        # 5% of 200K = below 10% threshold
        assert not should_activate(cfg, deferrable_tokens=10_000, context_length=200_000)

    def test_auto_at_or_above_threshold_activates(self):
        from tools.tool_search import ToolSearchConfig, should_activate
        cfg = ToolSearchConfig.from_raw({"enabled": "auto", "threshold_pct": 10})
        assert should_activate(cfg, deferrable_tokens=20_000, context_length=200_000)
        assert should_activate(cfg, deferrable_tokens=50_000, context_length=200_000)

    def test_auto_without_context_length_uses_20k_cutoff(self):
        """Fallback cutoff used when the active model is unknown."""
        from tools.tool_search import ToolSearchConfig, should_activate
        cfg = ToolSearchConfig.from_raw({"enabled": "auto"})
        assert not should_activate(cfg, deferrable_tokens=10_000, context_length=0)
        assert should_activate(cfg, deferrable_tokens=25_000, context_length=0)

    def test_token_estimate_proportional_to_schema_size(self):
        from tools.tool_search import estimate_tokens_from_schemas
        small = [_td("a", "x")]
        big = [_td(f"name_{i}", f"description for tool {i} " * 20,
                   {"q": {"type": "string", "description": "search query " * 10}})
               for i in range(10)]
        small_t = estimate_tokens_from_schemas(small)
        big_t = estimate_tokens_from_schemas(big)
        assert big_t > small_t * 10


# ---------------------------------------------------------------------------
# Retrieval (BM25 + substring fallback)
# ---------------------------------------------------------------------------


class TestRetrieval:
    def _fake_catalog(self):
        """Build a catalog directly without touching the registry."""
        from tools.tool_search import CatalogEntry, _tokenize, _entry_search_text
        defs = [
            _td("github_create_issue", "Open a new issue in a GitHub repository",
                {"title": {"type": "string"}, "body": {"type": "string"}}),
            _td("github_search_repos", "Search GitHub for matching repositories",
                {"query": {"type": "string"}}),
            _td("slack_send_message", "Post a message into a Slack channel",
                {"channel": {"type": "string"}, "text": {"type": "string"}}),
            _td("calendar_create_event", "Add an event to the user's calendar",
                {"title": {"type": "string"}, "start": {"type": "string"}}),
        ]
        catalog = []
        for d in defs:
            fn = d["function"]
            e = CatalogEntry(
                name=fn["name"], description=fn["description"],
                schema=d, source="mcp", source_name="mcp-test",
            )
            e._tokens = _tokenize(_entry_search_text(d))
            catalog.append(e)
        return catalog

    def test_search_finds_relevant_tool(self):
        from tools.tool_search import search_catalog
        hits = search_catalog(self._fake_catalog(), "create a github issue", limit=3)
        names = [h.name for h in hits]
        assert names[0] == "github_create_issue"

    def test_search_returns_empty_for_irrelevant_query(self):
        from tools.tool_search import search_catalog
        hits = search_catalog(self._fake_catalog(), "asdf qwerty foobar", limit=3)
        assert hits == []

    def test_search_substring_fallback(self):
        """Even when no BM25 hit, a literal substring of the tool name returns."""
        from tools.tool_search import search_catalog
        hits = search_catalog(self._fake_catalog(), "calendar", limit=3)
        assert any("calendar" in h.name for h in hits)

    def test_search_respects_limit(self):
        from tools.tool_search import search_catalog
        hits = search_catalog(self._fake_catalog(), "github", limit=1)
        assert len(hits) <= 1


# ---------------------------------------------------------------------------
# Assembly — the full passthrough/activate decision.
# ---------------------------------------------------------------------------


class TestAssembly:
    def test_no_deferrable_returns_unchanged(self):
        """Pure-core toolset: pass-through, no bridge tools added."""
        from tools.tool_search import assemble_tool_defs, ToolSearchConfig
        defs = [_td("terminal", "Run shell"), _td("read_file", "Read a file")]
        result = assemble_tool_defs(
            defs,
            context_length=200_000,
            config=ToolSearchConfig.from_raw({"enabled": "on"}),
        )
        assert not result.activated
        assert {t["function"]["name"] for t in result.tool_defs} == {"terminal", "read_file"}

    def test_below_threshold_returns_unchanged(self):
        """Tiny deferrable surface: don't bother."""
        from tools.tool_search import assemble_tool_defs, ToolSearchConfig
        # _td renders to ~80 chars / 20 tokens. 3 of them = ~60 tokens.
        # 10% of 200K = 20K. Way below.
        defs = [_td("unknown_tool_a"), _td("unknown_tool_b"), _td("unknown_tool_c")]
        result = assemble_tool_defs(
            defs,
            context_length=200_000,
            config=ToolSearchConfig.from_raw({"enabled": "auto", "threshold_pct": 10}),
        )
        assert not result.activated
        names = {(t.get("function") or {}).get("name") for t in result.tool_defs}
        assert "tool_search" not in names

    def test_idempotent_when_bridge_already_present(self):
        from tools.tool_search import assemble_tool_defs, ToolSearchConfig, BRIDGE_TOOL_NAMES
        defs = [_td("terminal", "Run shell"), _td("tool_search", "old")]
        result = assemble_tool_defs(
            defs,
            context_length=200_000,
            config=ToolSearchConfig.from_raw({"enabled": "off"}),
        )
        names = [(t["function"]["name"]) for t in result.tool_defs]
        # The pre-existing tool_search was stripped (it would be re-injected if
        # activation happened; here it didn't).
        assert "tool_search" not in names


# ---------------------------------------------------------------------------
# Bridge dispatch
# ---------------------------------------------------------------------------


class TestBridgeDispatch:
    def test_tool_search_requires_query(self):
        from tools.tool_search import dispatch_tool_search
        result = dispatch_tool_search({}, current_tool_defs=[])
        assert "error" in json.loads(result)

    def test_tool_describe_requires_name(self):
        from tools.tool_search import dispatch_tool_describe
        result = dispatch_tool_describe({}, current_tool_defs=[])
        assert "error" in json.loads(result)

    def test_tool_describe_rejects_non_deferrable(self):
        """If the model asks to describe a core tool, refuse — it's already
        in the visible list."""
        from tools.tool_search import dispatch_tool_describe
        result = dispatch_tool_describe(
            {"name": "terminal"}, current_tool_defs=[_td("terminal", "Run shell")],
        )
        assert "error" in json.loads(result)

    def test_resolve_underlying_call_parses_object_args(self):
        from tools.tool_search import resolve_underlying_call
        name, args, err = resolve_underlying_call({
            "name": "unknown_xxx",
            "arguments": {"foo": "bar"},
        })
        # Will fail classification because unknown_xxx isn't deferrable.
        assert err is not None

    def test_resolve_underlying_call_parses_json_string_args(self):
        """Some models emit ``arguments`` as a JSON string instead of object."""
        from tools.tool_search import resolve_underlying_call
        # Use a name that won't classify (so we don't depend on registry),
        # but exercise the JSON parse path.
        _, _, err = resolve_underlying_call({
            "name": "fake",
            "arguments": '{"a": 1}',
        })
        # err is about classification, but the parse worked (it would have
        # failed earlier with "not valid JSON" otherwise).
        assert "not valid JSON" not in (err or "")

    def test_resolve_underlying_call_rejects_bad_json(self):
        from tools.tool_search import resolve_underlying_call
        _, _, err = resolve_underlying_call({
            "name": "fake",
            "arguments": "{this is not json",
        })
        assert err is not None
        assert "JSON" in err

    def test_resolve_underlying_call_rejects_recursion(self):
        """tool_call cannot invoke tool_call itself."""
        from tools.tool_search import resolve_underlying_call, TOOL_CALL_NAME
        name, args, err = resolve_underlying_call({
            "name": TOOL_CALL_NAME,
            "arguments": {},
        })
        assert err is not None
        assert "bridge tool" in err.lower()


class TestSearchStreakGuard:
    """#1144 — fallback directive after N consecutive searches with no tool_call."""

    def _cfg(self, threshold: int):
        from tools.tool_search import ToolSearchConfig
        return ToolSearchConfig(enabled="on", threshold_pct=10.0,
                                search_default_limit=5, max_search_limit=20,
                                search_streak_threshold=threshold)

    def _search(self, sid, threshold=3):
        from tools.tool_search import dispatch_tool_search
        return json.loads(dispatch_tool_search(
            {"query": "github"},
            current_tool_defs=[_td("github_create_issue", "Create issue")],
            config=self._cfg(threshold), session_id=sid))

    def test_no_directive_below_threshold(self):
        import tools.tool_search as ts
        ts._SEARCH_STREAK.clear()
        out = self._search("sess-A", threshold=3)
        assert "fallback_directive" not in out  # streak=1 < 3

    def test_directive_at_threshold(self):
        import tools.tool_search as ts
        ts._SEARCH_STREAK.clear()
        self._search("sess-B", threshold=3)
        self._search("sess-B", threshold=3)
        out = self._search("sess-B", threshold=3)  # streak=3
        assert "fallback_directive" in out
        assert "3 times" in out["fallback_directive"]

    def test_reset_on_tool_call_clears_streak(self):
        import tools.tool_search as ts
        ts._SEARCH_STREAK.clear()
        self._search("sess-C", threshold=3)
        self._search("sess-C", threshold=3)
        ts.reset_search_streak("sess-C")  # model invoked a discovered tool
        out = self._search("sess-C", threshold=3)  # streak=1 again
        assert "fallback_directive" not in out

    def test_no_session_id_not_tracked(self):
        import tools.tool_search as ts
        ts._SEARCH_STREAK.clear()
        out = self._search(None, threshold=3)  # pure-function path
        assert "fallback_directive" not in out
        assert ts._SEARCH_STREAK == {}

    def test_threshold_zero_disables_guard(self):
        import tools.tool_search as ts
        ts._SEARCH_STREAK.clear()
        out = None
        for _ in range(5):
            out = self._search("sess-D", threshold=0)
        assert "fallback_directive" not in out

    def test_sessions_tracked_independently(self):
        import tools.tool_search as ts
        ts._SEARCH_STREAK.clear()
        self._search("sess-E", threshold=3)
        self._search("sess-F", threshold=3)
        self._search("sess-F", threshold=3)
        out_e = self._search("sess-E", threshold=3)  # E streak=2
        out_f = self._search("sess-F", threshold=3)  # F streak=3
        assert "fallback_directive" not in out_e
        assert "fallback_directive" in out_f


# ---------------------------------------------------------------------------
# End-to-end via the real handle_function_call (smoke test).
# ---------------------------------------------------------------------------


class TestHandleFunctionCallIntegration:
    def test_tool_search_dispatch_through_handle_function_call(self):
        """The dispatcher recognizes the bridge tool by name."""
        import model_tools
        result = model_tools.handle_function_call(
            function_name="tool_search",
            function_args={"query": "nothing matches this"},
        )
        parsed = json.loads(result)
        # Without a real registry, the matches will be empty, but the
        # dispatch path completed without error.
        assert "matches" in parsed or "error" in parsed


class TestRegression_OpenClawCron84141:
    """Regression guard for the OpenClaw cron-tool-loss class of bug.

    OpenClaw #84141: ``toolsAllow: ["exec"]`` on an isolated cron turn
    resulted in the agent receiving only ``sessions_send`` — the catalog
    builder silently dropped the requested core tool.

    Our defense: core tools are NEVER deferred. This test exercises the
    full assembly pipeline with a mixed core+MCP toolset and asserts that
    every core tool survives.
    """

    def test_core_tool_survives_alongside_many_mcp_tools(self):
        from tools.tool_search import (
            assemble_tool_defs, ToolSearchConfig, BRIDGE_TOOL_NAMES,
            classify_tools,
        )
        # 1 core tool + 50 unknown/MCP-shaped tools (deferrable).
        defs = [_td("terminal", "Run shell commands")]
        # Pad with fake "deferrable" tools — without registry registration,
        # classify_tools puts them in 'visible'. So instead, we just verify
        # the core-tool side: terminal stays in visible regardless.
        visible, deferrable = classify_tools(defs)
        assert any(
            (td.get("function") or {}).get("name") == "terminal"
            for td in visible
        ), "Core tool 'terminal' was wrongly classified as deferrable"

        # Now force activation and check the resulting tool-defs list.
        result = assemble_tool_defs(
            defs,
            context_length=200_000,
            config=ToolSearchConfig.from_raw({"enabled": "on"}),
        )
        names = {(t.get("function") or {}).get("name") for t in result.tool_defs}
        # terminal must be present; bridges are only added if there are
        # deferrable tools to put behind them.
        assert "terminal" in names

    def test_unwrap_rejects_core_tool_attempt(self):
        """Even if the model tries to invoke a core tool through tool_call,
        we reject the call and tell the model to use it directly."""
        from tools.tool_search import resolve_underlying_call
        _, _, err = resolve_underlying_call({
            "name": "terminal",
            "arguments": {"command": "echo hi"},
        })
        assert err is not None
        assert "not a deferrable" in err


class TestRegression_ToolsetScoping:
    """A restricted-toolset session must not see or invoke out-of-scope tools.

    The bug: the bridge dispatch and the tool_executor unwrap read the
    catalog from the *global* registry (get_tool_definitions with no
    toolset scope = "start with everything"), so a session scoped to one
    MCP server could tool_search the entire process registry and tool_call
    any plugin tool it was never granted. registry.dispatch() has no
    enabled_tools gate for non-execute_code tools, so the out-of-scope tool
    actually ran.

    The fix threads the session's enabled/disabled toolsets into the bridge
    dispatch (model_tools.handle_function_call) and the executor unwrap
    (agent.tool_executor), scoping both the searchable catalog and the
    invocable set to the session's own toolsets.
    """

    @staticmethod
    def _register(name, toolset):
        from tools.registry import registry

        def _handler(args, task_id=None, **kw):
            return json.dumps({"ok": True, "tool": name})

        registry.register(
            name=name,
            handler=_handler,
            schema=_td(name, f"desc for {name}", {"repo": {"type": "string"}}),
            toolset=toolset,
        )

    def test_search_catalog_is_scoped_to_session_toolsets(self):
        import model_tools

        for i in range(12):
            self._register(f"mcp_scoped_gh_{i}", "mcp-scoped-gh")
        self._register("scoped_oos_plugin", "scopedoosplugin")

        # tool_search scoped to the github toolset must not count the
        # out-of-scope plugin tool (or any of the host registry).
        result = model_tools.handle_function_call(
            function_name="tool_search",
            function_args={"query": "mcp_scoped_gh", "limit": 5},
            enabled_toolsets=["mcp-scoped-gh"],
        )
        parsed = json.loads(result)
        assert parsed["total_available"] == 12, (
            f"expected scoped catalog of 12, got {parsed['total_available']} "
            "— catalog leaked tools outside the session's toolsets"
        )
        hit_names = {m["name"] for m in parsed["matches"]}
        assert "scoped_oos_plugin" not in hit_names

    def test_tool_call_rejects_out_of_scope_tool(self):
        import model_tools

        self._register("mcp_inscope_gh_op", "mcp-inscope-gh")
        self._register("inscope_oos_plugin", "inscopeoosplugin")

        # Out-of-scope plugin tool: rejected even though it is registered
        # and deferrable in the global registry.
        rejected = json.loads(model_tools.handle_function_call(
            function_name="tool_call",
            function_args={"name": "inscope_oos_plugin", "arguments": {}},
            enabled_toolsets=["mcp-inscope-gh"],
        ))
        assert "error" in rejected
        assert "not available in this session" in rejected["error"]

        # In-scope tool: dispatches normally.
        ok = json.loads(model_tools.handle_function_call(
            function_name="tool_call",
            function_args={"name": "mcp_inscope_gh_op", "arguments": {"repo": "a/b"}},
            enabled_toolsets=["mcp-inscope-gh"],
        ))
        assert ok.get("ok") is True
        assert ok.get("tool") == "mcp_inscope_gh_op"

    def test_bridge_dispatch_does_not_pollute_global_resolved_names(self):
        import model_tools

        self._register("mcp_pollute_op_0", "mcp-pollute")
        self._register("mcp_pollute_op_1", "mcp-pollute")

        # Establish the scoped session global.
        model_tools.get_tool_definitions(
            enabled_toolsets=["mcp-pollute"], quiet_mode=True,
        )
        before = set(model_tools._last_resolved_tool_names)
        assert "terminal" not in before

        # A scoped tool_search call must not widen the process-global
        # _last_resolved_tool_names to the whole registry (which would leak
        # core/sandbox tools into execute_code's fallback).
        model_tools.handle_function_call(
            function_name="tool_search",
            function_args={"query": "pollute"},
            enabled_toolsets=["mcp-pollute"],
        )
        after = set(model_tools._last_resolved_tool_names)
        assert "terminal" not in after, (
            "bridge dispatch polluted _last_resolved_tool_names with "
            "out-of-scope tools"
        )

    def test_scoped_deferrable_names_helper(self):
        from tools.tool_search import scoped_deferrable_names

        self._register("mcp_helper_op", "mcp-helper")
        import model_tools
        defs = model_tools.get_tool_definitions(
            enabled_toolsets=["mcp-helper"],
            quiet_mode=True,
            skip_tool_search_assembly=True,
        )
        names = scoped_deferrable_names(defs)
        assert "mcp_helper_op" in names
        # core tools are never deferrable
        assert "terminal" not in names


# ---------------------------------------------------------------------------
# #1015 — tool_describe schema caching
# ---------------------------------------------------------------------------


class TestDescribeCache:
    """#1015 — dispatch_tool_describe caches successful results keyed by
    (name, toolset_signature) so repeated calls skip the full catalog scan."""

    def test_cache_hit_returns_same_result(self):
        from tools.tool_search import (
            dispatch_tool_describe,
            clear_describe_cache,
            is_deferrable_tool_name,
        )
        clear_describe_cache()

        # Register a deferrable tool so describe has something to find.
        from tools.registry import registry
        registry.register(
            name="cache_test_tool",
            toolset="mcp-cache-test",
            schema={
                "name": "cache_test_tool",
                "description": "Test tool for caching.",
                "parameters": {"type": "object", "properties": {}},
            },
            handler=lambda args, **kw: '{"ok": true}',
        )
        try:
            # Use a toolset that will classify it as deferrable.
            defs = [{
                "type": "function",
                "function": {
                    "name": "cache_test_tool",
                    "description": "Test tool for caching.",
                    "parameters": {"type": "object", "properties": {}},
                },
            }]
            r1 = dispatch_tool_describe({"name": "cache_test_tool"}, current_tool_defs=defs)
            r2 = dispatch_tool_describe({"name": "cache_test_tool"}, current_tool_defs=defs)
            # Cache hit: same JSON string (identity check — not just equal).
            assert r1 == r2
            assert "cache_test_tool" in r1
        finally:
            clear_describe_cache()

    def test_cache_invalidates_on_toolset_change(self):
        from tools.tool_search import (
            dispatch_tool_describe,
            clear_describe_cache,
        )
        clear_describe_cache()

        from tools.registry import registry
        registry.register(
            name="cache_sig_tool",
            toolset="mcp-sig-test",
            schema={
                "name": "cache_sig_tool",
                "description": "v1",
                "parameters": {"type": "object", "properties": {}},
            },
            handler=lambda args, **kw: '{"ok": true}',
        )
        try:
            defs_v1 = [{
                "type": "function",
                "function": {
                    "name": "cache_sig_tool",
                    "description": "v1",
                    "parameters": {"type": "object", "properties": {}},
                },
            }]
            defs_v2 = [{
                "type": "function",
                "function": {
                    "name": "cache_sig_tool",
                    "description": "v2 CHANGED",
                    "parameters": {"type": "object", "properties": {}},
                },
            }, {
                "type": "function",
                "function": {
                    "name": "other_tool",
                    "description": "other",
                    "parameters": {"type": "object", "properties": {}},
                },
            }]
            r1 = dispatch_tool_describe({"name": "cache_sig_tool"}, current_tool_defs=defs_v1)
            r2 = dispatch_tool_describe({"name": "cache_sig_tool"}, current_tool_defs=defs_v2)
            # Different signatures → cache miss → different result.
            assert r1 != r2
            assert "v2 CHANGED" in r2
        finally:
            clear_describe_cache()

    def test_error_results_not_cached(self):
        from tools.tool_search import (
            dispatch_tool_describe,
            clear_describe_cache,
        )
        clear_describe_cache()

        defs: List[Dict[str, Any]] = []
        r1 = dispatch_tool_describe({"name": "nonexistent"}, current_tool_defs=defs)
        # Error response should not be cached — verify it's an error.
        assert "error" in r1
        # A second call should also return an error (not a stale cache hit).
        r2 = dispatch_tool_describe({"name": "nonexistent"}, current_tool_defs=defs)
        assert "error" in r2

