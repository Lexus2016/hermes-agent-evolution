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


def _td(
    name: str, description: str = "", properties: Dict[str, Any] | None = None
) -> Dict[str, Any]:
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
        assert cfg.threshold_pct == 5.0

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
        assert (
            ToolSearchConfig.from_raw({"enabled": "on"}).defer_core_toolsets
            == frozenset()
        )

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

        # Sample of core tools from _HERMES_CORE_TOOLS that are never deferred by default.
        for core_name in [
            "terminal",
            "read_file",
            "write_file",
            "patch",
            "search_files",
            "memory",
            "browser_navigate",
            "web_search",
            "clarify",
            "execute_code",
            "delegate_task",
            "send_message",
        ]:
            assert not is_deferrable_tool_name(core_name), (
                f"Core tool '{core_name}' must NEVER be deferrable"
            )

    def test_bridge_tools_never_defer(self):
        from tools.tool_search import is_deferrable_tool_name, BRIDGE_TOOL_NAMES

        for name in BRIDGE_TOOL_NAMES:
            assert not is_deferrable_tool_name(name)

    def test_gui_surface_defers_by_default(self):
        """2026-08 core-deferral reversal: the curated defer set (GUI surface
        included) hides behind the bridge BY DEFAULT. project tools not in
        the defer set stay direct."""
        from tools.registry import discover_builtin_tools
        from tools.tool_search import ToolSearchConfig, assemble_tool_defs

        discover_builtin_tools()
        assembled = assemble_tool_defs(
            [_td(name, f"GUI {name}") for name in
             {"read_window_below", "apply_layout", "project_list"}],
            context_length=200_000,
            config=ToolSearchConfig.from_raw({"enabled": "on"}),
        )
        assert assembled.activated
        names = {td["function"]["name"] for td in assembled.tool_defs}
        assert "read_window_below" not in names
        assert "apply_layout" not in names
        # project_list is NOT in the curated defer set → stays direct.
        assert "project_list" in names

    def test_defer_override_restores_legacy_direct_gui(self):
        """tools.tool_search.defer: [] restores the everything-eager legacy:
        GUI tools alone no longer activate the bridge."""
        from tools.registry import discover_builtin_tools
        from tools.tool_search import ToolSearchConfig, assemble_tool_defs

        discover_builtin_tools()
        names = {"read_window_below", "apply_layout", "project_list"}
        assembled = assemble_tool_defs(
            [_td(name, f"GUI {name}") for name in names],
            context_length=200_000,
            config=ToolSearchConfig.from_raw({"enabled": "on", "defer": []}),
        )
        assert not assembled.activated
        assert {td["function"]["name"] for td in assembled.tool_defs} == names

    def test_core_working_set_never_defers_even_with_mcp_active(self):
        """The bridge activates for MCP, but working-set core tools (terminal,
        files, memory...) stay direct — the deferral set is the CURATED list,
        not all of core."""
        from tools.registry import discover_builtin_tools, registry
        from tools.tool_search import (
            BRIDGE_TOOL_NAMES,
            ToolSearchConfig,
            assemble_tool_defs,
        )

        discover_builtin_tools()
        mcp_name = "mcp_gui_surface_probe"
        registry.register(
            name=mcp_name,
            handler=lambda args, **kw: "{}",
            schema=_td(mcp_name, "Deferred MCP capability")["function"],
            toolset="mcp-gui-surface-probe",
        )

        assembled = assemble_tool_defs(
            [
                _td("terminal", "Run a command"),
                _td("memory", "Persistent memory"),
                _td("computer_use", "Drive the OS"),
                _td(mcp_name, "Deferred MCP capability"),
            ],
            context_length=200_000,
            config=ToolSearchConfig.from_raw({"enabled": "on"}),
        )
        names = {td["function"]["name"] for td in assembled.tool_defs}

        assert assembled.activated
        assert mcp_name not in names
        assert BRIDGE_TOOL_NAMES <= names
        assert {"terminal", "memory"} <= names
        # computer_use IS in the curated defer set → behind the bridge.
        assert "computer_use" not in names

    def test_clarify_stays_eager_by_default(self):
        """PR #97979 A/B verdict (288 runs, 3 model tiers): clarify deferred
        collapsed structured ask-the-user usage 18/18 → 7/18 (gpt-terra 0/6);
        models fell back to plain-text questions. The ask-the-user affordance
        must stay ambient — clarify is NOT in the curated default defer set,
        and assembles as a direct tool even when the bridge is active."""
        from tools.registry import discover_builtin_tools
        from tools.tool_search import (
            _DEFAULT_DEFERRED_TOOLS,
            ToolSearchConfig,
            assemble_tool_defs,
        )

        assert "clarify" not in _DEFAULT_DEFERRED_TOOLS

        discover_builtin_tools()
        assembled = assemble_tool_defs(
            [
                _td("clarify", "Ask the user clarifying questions"),
                _td("computer_use", "Drive the OS"),
            ],
            context_length=200_000,
            config=ToolSearchConfig.from_raw({"enabled": "on"}),
        )
        assert assembled.activated  # computer_use still activates the bridge
        names = {td["function"]["name"] for td in assembled.tool_defs}
        assert "clarify" in names
        assert "computer_use" not in names

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

        model_tools.get_tool_definitions(
            quiet_mode=True, skip_tool_search_assembly=True
        )

    def _cfg(self, **over):
        from tools.tool_search import ToolSearchConfig

        raw = {"enabled": "on", "defer_core_toolsets": [self._DEMO_TOOLSET]}
        raw.update(over)
        return ToolSearchConfig.from_raw(raw)

    def test_effective_core_unchanged_by_default(self):
        from tools.tool_search import (
            effective_core_tool_names,
            _hermes_core_tools,
            ToolSearchConfig,
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

        defs = (
            model_tools.get_tool_definitions(
                quiet_mode=True,
                skip_tool_search_assembly=True,
            )
            or []
        )
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

        defs = (
            model_tools.get_tool_definitions(
                quiet_mode=True,
                skip_tool_search_assembly=True,
            )
            or []
        )
        baseline = assemble_tool_defs(
            defs,
            context_length=200_000,
            config=ToolSearchConfig.from_raw({"enabled": "on"}),
        )
        opted = assemble_tool_defs(defs, context_length=200_000, config=self._cfg())
        # Opting browser in must add browser_* to the deferred count and
        # the measured deferred-token total grows accordingly.
        assert opted.deferred_count > baseline.deferred_count
        assert opted.deferred_tokens > baseline.deferred_tokens
        result_names = {
            (td.get("function") or {}).get("name") for td in opted.tool_defs
        }
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
            scoped_deferrable_names,
            resolve_underlying_call,
            dispatch_tool_describe,
        )

        cfg = self._cfg()
        defs = (
            model_tools.get_tool_definitions(
                quiet_mode=True,
                skip_tool_search_assembly=True,
            )
            or []
        )
        scope = scoped_deferrable_names(defs, cfg)
        assert self._DEMO_TOOL in scope
        name, _args, err = resolve_underlying_call(
            {"name": self._DEMO_TOOL, "arguments": {}},
            cfg,
        )
        assert err is None and name == self._DEMO_TOOL
        described = json.loads(
            dispatch_tool_describe(
                {"names": [self._DEMO_TOOL]},
                current_tool_defs=defs,
                config=cfg,
            )
        )
        assert "parameters" in described.get("tools", {}).get(self._DEMO_TOOL, {})

    def test_default_config_keeps_native_tool_direct(self):
        """The inverse of the round-trip: with no opt-in, the core tool stays
        direct and the bridge refuses to resolve it (use it directly)."""
        from tools.tool_search import ToolSearchConfig, resolve_underlying_call

        cfg = ToolSearchConfig.from_raw({"enabled": "on"})
        _name, _args, err = resolve_underlying_call(
            {"name": self._DEMO_TOOL, "arguments": {}},
            cfg,
        )
        assert err is not None
        assert "not a deferrable" in err

    def test_naming_non_core_toolset_is_a_noop(self):
        """Opting in a toolset with no core members changes nothing — those
        tools were already deferrable (or non-existent)."""
        from tools.tool_search import (
            effective_core_tool_names,
            _hermes_core_tools,
            ToolSearchConfig,
        )

        cfg = ToolSearchConfig.from_raw({
            "enabled": "on",
            "defer_core_toolsets": ["xx_no_such_toolset"],
        })
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
        assert not should_activate(
            cfg, deferrable_tokens=1_000_000, context_length=200_000
        )

    def test_zero_deferrable_never_activates(self):
        from tools.tool_search import ToolSearchConfig, should_activate

        cfg = ToolSearchConfig.from_raw({"enabled": "on"})
        assert not should_activate(cfg, deferrable_tokens=0, context_length=200_000)

    def test_on_activates_with_any_deferrable(self):
        from tools.tool_search import ToolSearchConfig, should_activate

        cfg = ToolSearchConfig.from_raw({"enabled": "on"})
        assert should_activate(cfg, deferrable_tokens=100, context_length=200_000)

    def test_threshold_no_longer_gates_activation(self):
        """threshold_pct governs the LISTING BUDGET, not activation.

        Upstream's July 2026 tiered-disclosure change: any deferrable tool
        activates the bridge, because schemas always defer — there is nothing
        to gain by leaving a deferrable tool inline. What threshold_pct now
        sizes is how much of the catalog listing gets embedded
        (listing_token_budget). This used to assert the opposite.
        """
        from tools.tool_search import (
            ToolSearchConfig,
            listing_token_budget,
            should_activate,
        )

        cfg = ToolSearchConfig.from_raw({"enabled": "auto", "threshold_pct": 10})
        # Well under 10% of the context, yet it still activates.
        assert should_activate(cfg, deferrable_tokens=10_000, context_length=200_000)
        # The percentage shows up in the listing budget, but capped by
        # listing_max_tokens (default 4000): min(4000, 10% * 200000) = 4000.
        assert listing_token_budget(cfg, 200_000) == 4_000

    def test_auto_at_or_above_threshold_activates(self):
        from tools.tool_search import ToolSearchConfig, should_activate

        cfg = ToolSearchConfig.from_raw({"enabled": "auto", "threshold_pct": 10})
        assert should_activate(cfg, deferrable_tokens=20_000, context_length=200_000)
        assert should_activate(cfg, deferrable_tokens=50_000, context_length=200_000)

    def test_activation_needs_only_a_deferrable_tool(self):
        """With no known context length, activation still only needs a tool.

        The old fallback cutoff gated activation; under tiered disclosure the
        unknown-context fallback applies to the listing budget instead.
        """
        from tools.tool_search import (
            ToolSearchConfig,
            listing_token_budget,
            should_activate,
        )

        cfg = ToolSearchConfig.from_raw({"enabled": "auto"})
        assert should_activate(cfg, deferrable_tokens=10_000, context_length=0)
        assert should_activate(cfg, deferrable_tokens=25_000, context_length=0)
        # Nothing deferrable is still a no-op.
        assert not should_activate(cfg, deferrable_tokens=0, context_length=0)
        # Unknown context falls back to a fixed cutoff for the listing budget.
        assert listing_token_budget(cfg, 0) > 0

    def test_token_estimate_proportional_to_schema_size(self):
        from tools.tool_search import estimate_tokens_from_schemas

        small = [_td("a", "x")]
        big = [
            _td(
                f"name_{i}",
                f"description for tool {i} " * 20,
                {"q": {"type": "string", "description": "search query " * 10}},
            )
            for i in range(10)
        ]
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
            _td(
                "github_create_issue",
                "Open a new issue in a GitHub repository",
                {"title": {"type": "string"}, "body": {"type": "string"}},
            ),
            _td(
                "github_search_repos",
                "Search GitHub for matching repositories",
                {"query": {"type": "string"}},
            ),
            _td(
                "slack_send_message",
                "Post a message into a Slack channel",
                {"channel": {"type": "string"}, "text": {"type": "string"}},
            ),
            _td(
                "calendar_create_event",
                "Add an event to the user's calendar",
                {"title": {"type": "string"}, "start": {"type": "string"}},
            ),
        ]
        catalog = []
        for d in defs:
            fn = d["function"]
            e = CatalogEntry(
                name=fn["name"],
                description=fn["description"],
                schema=d,
                source="mcp",
                source_name="mcp-test",
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
        assert {t["function"]["name"] for t in result.tool_defs} == {
            "terminal",
            "read_file",
        }

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
        from tools.tool_search import (
            assemble_tool_defs,
            ToolSearchConfig,
            BRIDGE_TOOL_NAMES,
        )

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
    def test_tool_search_requires_queries(self):
        from tools.tool_search import dispatch_tool_search

        result = dispatch_tool_search({}, current_tool_defs=[])
        assert "error" in json.loads(result)

    def test_tool_search_rejects_empty_and_overcap_queries(self):
        import tools.tool_search as tool_search

        cfg = tool_search.ToolSearchConfig.from_raw({})
        assert "error" in json.loads(tool_search.dispatch_tool_search(
            {"queries": []}, current_tool_defs=[], config=cfg))
        assert "error" in json.loads(tool_search.dispatch_tool_search(
            {"queries": ["  ", ""]}, current_tool_defs=[], config=cfg))
        over = ["q"] * (tool_search._MAX_QUERIES_PER_CALL + 1)
        parsed = json.loads(tool_search.dispatch_tool_search(
            {"queries": over}, current_tool_defs=[], config=cfg))
        assert "error" in parsed
        assert "too many queries" in parsed["error"]

    def test_empty_search_keeps_connected_sources_discoverable(self):
        from tools.registry import registry
        from tools.tool_search import dispatch_tool_search

        name = "recovery_catalog_create_record"
        tool_def = _td(name, "Create a record in the connected catalog service.")
        registry.register(
            name=name,
            handler=lambda args, **kwargs: "{}",
            schema=tool_def,
            toolset="mcp-recovery-catalog",
        )

        result = json.loads(dispatch_tool_search(
            {"queries": ["unrelated vocabulary"]},
            current_tool_defs=[tool_def],
        ))

        [group] = result["results"]
        assert group["query"] == "unrelated vocabulary"
        assert group["matches"] == []
        assert result["tools"] == {}
        assert result["total_available"] == 1
        assert group["available_sources"] == [
            {"name": "recovery-catalog", "tool_count": 1},
        ]
        assert "remain available" in group["hint"]
        assert "before concluding" in group["hint"]
        assert "available_sources" not in result

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

    def test_resolve_underlying_call_treats_empty_string_args_as_empty_dict(self):
        """#1173 — some providers (e.g. GLM-5.2 via OpenRouter) emit
        ``arguments: ""`` for no-parameter tools. An empty string is not
        malformed JSON that should be rejected; it is the absence of
        arguments and must resolve to ``{}`` so the underlying tool can
        be dispatched. Previously this hit ``json.loads("")`` and surfaced
        a confusing "not valid JSON" error to the model, which then
        looped on the same call."""
        from tools.tool_search import resolve_underlying_call

        name, args, err = resolve_underlying_call({
            "name": "fake_no_params_tool",
            "arguments": "",
        })
        # The empty string is accepted as {} — the only error, if any,
        # is the (expected) deferrability classification failure, never
        # a JSON parse error.
        assert "not valid JSON" not in (err or "")
        assert args == {}

    @pytest.mark.parametrize("blank", ["", " ", "   ", "\n", "\t", " \t\n "])
    def test_resolve_underlying_call_treats_whitespace_only_args_as_empty_dict(
        self, blank
    ):
        """Follow-up to #1173 — providers (and intermediary gateways) can
        emit not just ``""`` but whitespace-only ``arguments`` (a stray
        space or newline from tokenization). These are equally the absence
        of arguments and must resolve to ``{}``, never a JSON parse error
        that the model loops on. ``json.loads`` trims whitespace around a
        real value, so accepting whitespace-only cannot mask a valid
        payload."""
        from tools.tool_search import resolve_underlying_call

        name, args, err = resolve_underlying_call({
            "name": "fake_no_params_tool",
            "arguments": blank,
        })
        assert "not valid JSON" not in (err or "")
        assert args == {}
        """tool_call cannot invoke tool_call itself."""
        from tools.tool_search import resolve_underlying_call, TOOL_CALL_NAME

        name, args, err = resolve_underlying_call({
            "name": TOOL_CALL_NAME,
            "arguments": {},
        })
        assert err is not None
        assert "bridge tool" in err.lower()

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

        return ToolSearchConfig(
            enabled="on",
            threshold_pct=10.0,
            search_default_limit=5,
            max_search_limit=20,
            search_streak_threshold=threshold,
        )

    def _search(self, sid, threshold=3):
        from tools.tool_search import dispatch_tool_search

        return json.loads(
            dispatch_tool_search(
                {"queries": ["github"]},
                current_tool_defs=[_td("github_create_issue", "Create issue")],
                config=self._cfg(threshold),
                session_id=sid,
            )
        )

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
            function_args={"queries": ["nothing matches this"]},
        )
        parsed = json.loads(result)
        # Without a real registry, the matches will be empty, but the
        # dispatch path completed without error.
        assert "results" in parsed or "error" in parsed

    def test_tool_search_emits_one_terminal_hook(self, monkeypatch):
        """Inline bridge results still complete the tool lifecycle."""
        import model_tools
        from hermes_cli import lifecycle
        from tools import tool_search

        events = []
        monkeypatch.setattr(
            lifecycle,
            "has_hook",
            lambda name: name == "post_tool_call",
        )
        monkeypatch.setattr(
            lifecycle,
            "invoke_hook",
            lambda name, **kwargs: events.append((name, kwargs)),
        )
        monkeypatch.setattr(
            tool_search,
            "dispatch_tool_search",
            lambda *args, **kwargs: json.dumps({"results": []}),
        )

        result = model_tools.handle_function_call(
            function_name="tool_search",
            function_args={"queries": ["private-query"]},
            session_id="private-session",
            task_id="private-task",
            turn_id="private-turn",
            api_request_id="private-request",
            tool_call_id="private-call",
        )

        assert json.loads(result) == {"results": []}
        assert len(events) == 1
        hook_name, payload = events[0]
        assert hook_name == "post_tool_call"
        assert payload["status"] == "ok"
        assert payload["turn_id"] == "private-turn"
        assert payload["api_request_id"] == "private-request"
        assert payload["tool_call_id"] == "private-call"


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
            assemble_tool_defs,
            ToolSearchConfig,
            BRIDGE_TOOL_NAMES,
            classify_tools,
        )

        # 1 core tool + 50 unknown/MCP-shaped tools (deferrable).
        defs = [_td("terminal", "Run shell commands")]
        # Pad with fake "deferrable" tools — without registry registration,
        # classify_tools puts them in 'visible'. So instead, we just verify
        # the core-tool side: terminal stays in visible regardless.
        visible, deferrable = classify_tools(defs)
        assert any(
            (td.get("function") or {}).get("name") == "terminal" for td in visible
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
            function_args={"queries": ["mcp_scoped_gh"], "limit": 5},
            enabled_toolsets=["mcp-scoped-gh"],
        )
        parsed = json.loads(result)
        assert parsed["total_available"] == 12, (
            f"expected scoped catalog of 12, got {parsed['total_available']} "
            "— catalog leaked tools outside the session's toolsets"
        )
        hit_names = set(parsed["tools"])
        assert hit_names == {n for g in parsed["results"] for n in g["matches"]}
        assert "scoped_oos_plugin" not in hit_names

    def test_tool_call_rejects_out_of_scope_tool(self):
        import model_tools

        self._register("mcp_inscope_gh_op", "mcp-inscope-gh")
        self._register("inscope_oos_plugin", "inscopeoosplugin")

        # Out-of-scope plugin tool: rejected even though it is registered
        # and deferrable in the global registry.
        rejected = json.loads(
            model_tools.handle_function_call(
                function_name="tool_call",
                function_args={"name": "inscope_oos_plugin", "arguments": {}},
                enabled_toolsets=["mcp-inscope-gh"],
            )
        )
        assert "error" in rejected
        assert "not available in this session" in rejected["error"]

        # In-scope tool: dispatches normally.
        ok = json.loads(
            model_tools.handle_function_call(
                function_name="tool_call",
                function_args={
                    "name": "mcp_inscope_gh_op",
                    "arguments": {"repo": "a/b"},
                },
                enabled_toolsets=["mcp-inscope-gh"],
            )
        )
        assert ok.get("ok") is True
        assert ok.get("tool") == "mcp_inscope_gh_op"

    def test_bridge_dispatch_does_not_pollute_global_resolved_names(self):
        import model_tools

        self._register("mcp_pollute_op_0", "mcp-pollute")
        self._register("mcp_pollute_op_1", "mcp-pollute")

        # Establish the scoped session global.
        model_tools.get_tool_definitions(
            enabled_toolsets=["mcp-pollute"],
            quiet_mode=True,
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
            "bridge dispatch polluted _last_resolved_tool_names with out-of-scope tools"
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


class TestCatalogListing:
    def test_config_defaults(self):
        from tools.tool_search import ToolSearchConfig
        cfg = ToolSearchConfig.from_raw(None)
        assert cfg.listing == "auto"
        assert cfg.listing_max_tokens == 4000
        # legacy bool shapes keep defaults too
        assert ToolSearchConfig.from_raw(True).listing == "auto"

    def test_default_listing_cap_bounds_fixed_catalog_overhead(self):
        """The default manifest must not grow back to the old 20K-token cap."""
        from tools.registry import registry
        from tools.tool_search import (
            ToolSearchConfig,
            assemble_tool_defs,
            estimate_tokens_from_schemas,
        )

        defs = []
        for i in range(500):
            name = f"lean_catalog_tool_{i:04d}"
            registry.register(
                name=name,
                handler=lambda args, **kwargs: "{}",
                schema=_td(name, "Perform a deliberately verbose connected service action."),
                toolset="mcp-lean-catalog",
            )
            defs.append(_td(name, "Perform a deliberately verbose connected service action."))

        cfg = ToolSearchConfig.from_raw(None)
        result = assemble_tool_defs(defs, context_length=1_000_000, config=cfg)
        search = next(
            td for td in result.tool_defs
            if td["function"]["name"] == "tool_search"
        )
        description_tokens = estimate_tokens_from_schemas([search])
        # Includes the bridge schema around the listing, so allow modest
        # framing overhead above the 4K listing budget.
        assert description_tokens < 4500
        assert result.listing_form in {"names", "groups", "mixed"}

    def test_short_desc_first_sentence_and_clip(self):
        from tools.tool_search import _short_desc
        assert _short_desc("Open an issue. Second sentence dropped.") == "Open an issue."
        long = "word " * 40
        s = _short_desc(long)
        assert len(s) <= 61  # 60 + ellipsis char
        assert s.endswith("…")
        assert _short_desc("") == ""


class TestDeferredCallSchemaProbe:
    """Blind tool_call invocations missing required arguments must return
    the tool's parameter schema instead of dispatching into an opaque
    downstream failure (port of nearai/ironclaw#5149's describe-first fix).

    A deferred tool's schema is invisible until tool_describe is called, so
    models routinely invoke deferred tools by name alone. Pre-fix, that
    produced ``KeyError: 'document_id'``-style errors that teach the model
    nothing; post-fix, the probe returns the schema so the model repairs
    the call in one round-trip. Valid calls dispatch untouched.
    """

    @staticmethod
    def _register(name, toolset, required=("document_id",)):
        from tools.registry import registry

        def _handler(args, task_id=None, **kw):
            # Simulates a tool that crashes opaquely on a missing required arg.
            return json.dumps({"ok": True, "doc": args["document_id"]})

        params = {
            "type": "object",
            "properties": {
                "document_id": {"type": "string", "description": "Doc id"},
                "format": {"type": "string"},
            },
            "required": list(required),
        }
        registry.register(
            name=name,
            handler=_handler,
            schema={"name": name, "description": f"desc {name}",
                    "parameters": params},
            toolset=toolset,
        )

    @staticmethod
    def _register_schema(name, toolset, params, calls):
        from tools.registry import registry

        def _handler(args, task_id=None, **kw):
            calls.append(args)
            return json.dumps({"ok": True, "args": args})

        registry.register(
            name=name,
            handler=_handler,
            schema={"name": name, "description": f"desc {name}",
                    "parameters": params},
            toolset=toolset,
        )

    def test_validator_returns_schema_for_missing_required(self):
        from tools.tool_search import validate_deferred_call_args

        self._register("mcp_probe_docs_get", "mcp-probe")
        err = validate_deferred_call_args("mcp_probe_docs_get", {})
        assert err is not None
        parsed = json.loads(err)
        assert "document_id" in parsed["error"]
        assert "NOT invoked" in parsed["error"]
        assert parsed["parameters"]["required"] == ["document_id"]
        assert "document_id" in parsed["parameters"]["properties"]

    def test_validator_never_blocks_unvalidatable_tools(self):
        from tools.tool_search import validate_deferred_call_args

        # Unknown tool → no schema → dispatch (downstream scope gate handles it).
        assert validate_deferred_call_args("mcp_no_such_tool_xyz", {}) is None

    def test_valid_tool_call_still_dispatches(self):
        import model_tools

        self._register("mcp_probe_valid_op", "mcp-probe-valid")
        result = json.loads(model_tools.handle_function_call(
            function_name="tool_call",
            function_args={"name": "mcp_probe_valid_op",
                           "arguments": {"document_id": "abc"}},
            enabled_toolsets=["mcp-probe-valid"],
        ))
        assert result.get("ok") is True
        assert result.get("doc") == "abc"

    def test_invalid_enum_is_blocked_before_dispatch(self):
        import model_tools

        calls = []
        name = "mcp_probe_enum_validation"
        toolset = "mcp-probe-enum-validation"
        self._register_schema(name, toolset, {
            "type": "object",
            "properties": {
                "priority": {"type": "string", "enum": ["low", "high"]},
            },
            "required": ["priority"],
        }, calls)

        result = json.loads(model_tools.handle_function_call(
            function_name="tool_call",
            function_args={"name": name, "arguments": {"priority": "urgent"}},
            enabled_toolsets=[toolset],
        ))

        assert calls == []
        assert result["path"] == "arguments.priority"
        assert result["constraint"] == "enum"
        assert "NOT invoked" in result["error"]

    @pytest.mark.parametrize(
        ("suffix", "arguments", "expected_path", "expected_constraint"),
        [
            (
                "nested_type",
                {"options": {"count": "not-an-integer"}},
                "arguments.options.count",
                "type",
            ),
            (
                "nested_required",
                {"options": {}},
                "arguments.options",
                "required",
            ),
            (
                "nested_extra",
                {"options": {"count": 1, "extra": True}},
                "arguments.options",
                "additionalProperties",
            ),
        ],
    )
    def test_validator_reports_nested_constraint_path(
        self, suffix, arguments, expected_path, expected_constraint,
    ):
        from tools.tool_search import validate_deferred_call_args

        calls = []
        name = f"mcp_probe_{suffix}"
        self._register_schema(name, "mcp-probe-nested", {
            "type": "object",
            "properties": {
                "options": {
                    "type": "object",
                    "properties": {"count": {"type": "integer"}},
                    "required": ["count"],
                    "additionalProperties": False,
                },
            },
            "required": ["options"],
        }, calls)

        result = json.loads(validate_deferred_call_args(name, arguments))

        assert result["path"] == expected_path
        assert result["constraint"] == expected_constraint

    def test_coercible_arguments_validate_then_dispatch_repaired(self):
        import model_tools

        calls = []
        name = "mcp_probe_coercion_validation"
        toolset = "mcp-probe-coercion-validation"
        self._register_schema(name, toolset, {
            "type": "object",
            "properties": {"count": {"type": "integer"}},
            "required": ["count"],
        }, calls)

        result = json.loads(model_tools.handle_function_call(
            function_name="tool_call",
            function_args={"name": name, "arguments": {"count": "42"}},
            enabled_toolsets=[toolset],
        ))

        assert result["ok"] is True
        assert calls == [{"count": 42}]

    def test_nullable_extension_remains_accepted(self):
        import model_tools

        calls = []
        name = "mcp_probe_nullable_validation"
        toolset = "mcp-probe-nullable-validation"
        self._register_schema(name, toolset, {
            "type": "object",
            "properties": {"value": {"type": "string", "nullable": True}},
            "required": ["value"],
        }, calls)

        result = json.loads(model_tools.handle_function_call(
            function_name="tool_call",
            function_args={"name": name, "arguments": {"value": None}},
            enabled_toolsets=[toolset],
        ))

        assert result["ok"] is True
        assert calls == [{"value": None}]

    def test_schema_normalization_preserves_literal_enum_objects(self):
        from tools.tool_search import validate_deferred_call_args

        calls = []
        name = "mcp_probe_literal_enum_validation"
        enum_value = {"nullable": True, "$ref": "literal-not-a-schema"}
        self._register_schema(name, "mcp-probe-literal-enum", {
            "type": "object",
            "properties": {"value": {"enum": [enum_value]}},
            "required": ["value"],
        }, calls)

        assert validate_deferred_call_args(name, {"value": enum_value}) is None

    def test_malformed_schema_fails_open(self):
        import model_tools

        calls = []
        name = "mcp_probe_malformed_validation"
        toolset = "mcp-probe-malformed-validation"
        self._register_schema(name, toolset, {
            "type": "object",
            "properties": {"value": {"type": "not-a-json-schema-type"}},
        }, calls)

        result = json.loads(model_tools.handle_function_call(
            function_name="tool_call",
            function_args={"name": name, "arguments": {"value": "kept"}},
            enabled_toolsets=[toolset],
        ))

        assert result["ok"] is True
        assert calls == [{"value": "kept"}]

    def test_external_ref_fails_open_without_resolution(self):
        from tools.tool_search import validate_deferred_call_args

        calls = []
        name = "mcp_probe_external_ref_validation"
        self._register_schema(name, "mcp-probe-external-ref", {
            "type": "object",
            "properties": {
                "payload": {"$ref": "https://example.invalid/schema.json"},
            },
        }, calls)

        assert validate_deferred_call_args(name, {"payload": {"anything": True}}) is None


class TestSearchStreakEmptySessionNowFires:
    """#1373 — an empty-string session id (the runtime's
    ``agent.session_id or ""``) used to silently disable the streak counter,
    which is how #1153 shipped without moving its own signal. The counter now
    falls back to a stable default key so the feature actually fires in that
    environment. Only an explicit ``None`` (the pure-function test path) opts
    out of tracking.
    """

    # A deferrable (MCP-prefixed) tool registered with the live registry so
    # classify_tools / build_catalog treat it as deferrable and it appears in
    # the catalog, hits, full_tool_list, and auto_describe.
    _DEF_TOOL = "mcp_test__searchme"

    def _cfg(self, threshold: int = 3, describe_threshold: int = 5):
        from tools.tool_search import ToolSearchConfig

        return ToolSearchConfig(
            enabled="on",
            threshold_pct=10.0,
            search_default_limit=5,
            max_search_limit=20,
            search_streak_threshold=threshold,
            search_streak_describe_threshold=describe_threshold,
        )

    def _ensure_registered(self):
        """Register the deferrable test tool (idempotent)."""
        from tools.registry import registry

        try:
            if registry.get_entry(self._DEF_TOOL) is None:
                registry.register(
                    name=self._DEF_TOOL,
                    toolset="mcp-test",
                    schema={"type": "function"},
                    handler=lambda **kw: "{}",
                    description="A test MCP tool",
                )
        except Exception:
            pass

    def _tool_defs(self):
        self._ensure_registered()
        return [
            {
                "type": "function",
                "function": {
                    "name": self._DEF_TOOL,
                    "description": "A test MCP tool",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ]

    def _search(self, sid, threshold=3, describe_threshold=5, query="searchme"):
        from tools.tool_search import dispatch_tool_search

        return json.loads(
            dispatch_tool_search(
                {"queries": [query]},
                current_tool_defs=self._tool_defs(),
                config=self._cfg(threshold, describe_threshold),
                session_id=sid,
            )
        )

    def test_empty_session_id_increments_streak(self):
        """Empty-string session id must now be tracked, not silently dropped."""
        import tools.tool_search as ts

        ts._SEARCH_STREAK.clear()
        ts._SEARCH_QUERIES.clear()
        # Two sub-threshold calls, then the third trips the directive.
        self._search("", query="q1")
        self._search("", query="q2")
        out = self._search("", query="q3")
        assert "fallback_directive" in out
        assert "3 times" in out["fallback_directive"]

    def test_none_session_id_still_untracked(self):
        """Explicit None remains the pure-function opt-out path."""
        import tools.tool_search as ts

        ts._SEARCH_STREAK.clear()
        ts._SEARCH_QUERIES.clear()
        for _ in range(5):
            out = self._search(None)
        assert "fallback_directive" not in out
        assert ts._SEARCH_STREAK == {}

    def test_full_tool_list_injected_at_threshold(self):
        """At streak==3 the full deferrable tool list is injected."""
        import tools.tool_search as ts

        ts._SEARCH_STREAK.clear()
        ts._SEARCH_QUERIES.clear()
        self._search("sess-full", query="a")
        self._search("sess-full", query="b")
        out = self._search("sess-full", query="c")
        assert "full_tool_list" in out
        assert self._DEF_TOOL in out["full_tool_list"]

    def test_previous_queries_injected_at_threshold(self):
        """At streak==3 previous queries are surfaced so the model sees loops."""
        import tools.tool_search as ts

        ts._SEARCH_STREAK.clear()
        ts._SEARCH_QUERIES.clear()
        self._search("sess-prev", query="alpha")
        self._search("sess-prev", query="beta")
        out = self._search("sess-prev", query="gamma")
        assert "previous_queries" in out
        # The current query (gamma) must not appear in previous_queries.
        assert "gamma" not in out["previous_queries"]
        assert "alpha" in out["previous_queries"]
        assert "beta" in out["previous_queries"]

    def test_auto_describe_fires_at_describe_threshold(self):
        """At streak==5 the top hit is auto-described inline."""
        import tools.tool_search as ts

        ts._SEARCH_STREAK.clear()
        ts._SEARCH_QUERIES.clear()
        out = None
        for _ in range(5):
            # Use a query that actually matches the deferrable tool so hits is
            # non-empty and auto_describe has a top result to describe.
            out = self._search("sess-desc", query="searchme")
        assert out is not None
        assert "auto_describe" in out
        assert out["auto_describe"]["name"] == self._DEF_TOOL
        assert "parameters" in out["auto_describe"]

    def test_auto_describe_absent_below_describe_threshold(self):
        """At streak==3 (below 5) auto_describe must NOT be present."""
        import tools.tool_search as ts

        ts._SEARCH_STREAK.clear()
        ts._SEARCH_QUERIES.clear()
        self._search("sess-nodesc", query="a")
        self._search("sess-nodesc", query="b")
        out = self._search("sess-nodesc", query="c")
        assert "fallback_directive" in out
        assert "auto_describe" not in out

    def test_streak_resets_on_tool_call(self):
        """reset_search_streak clears the counter so the directive goes away."""
        import tools.tool_search as ts

        ts._SEARCH_STREAK.clear()
        ts._SEARCH_QUERIES.clear()
        self._search("sess-reset", query="a")
        self._search("sess-reset", query="b")
        self._search("sess-reset", query="c")  # streak 3 -> directive
        ts.reset_search_streak("sess-reset")  # model invoked a tool
        out = self._search("sess-reset", query="d")  # streak 1 again
        assert "fallback_directive" not in out

    def test_reset_clears_previous_queries(self):
        """reset_search_streak also clears the rolling query history."""
        import tools.tool_search as ts

        ts._SEARCH_STREAK.clear()
        ts._SEARCH_QUERIES.clear()
        self._search("sess-pq", query="a")
        self._search("sess-pq", query="b")
        self._search("sess-pq", query="c")
        assert ts.get_previous_queries("sess-pq") == ["a", "b", "c"]
        ts.reset_search_streak("sess-pq")
        assert ts.get_previous_queries("sess-pq") == []
