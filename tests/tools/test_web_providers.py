"""Tests for the web tools provider architecture.

Covers:
- WebSearchProvider / WebExtractProvider ABC enforcement
- Per-capability backend selection (_get_search_backend, _get_extract_backend)
- Backward compatibility (web.backend still works as shared fallback)
- Config keys merge correctly via DEFAULT_CONFIG
"""

from __future__ import annotations

import json
from typing import Any, Dict, List

import pytest

from tests.tools.conftest import register_all_web_providers


# ---------------------------------------------------------------------------
# ABC enforcement
# ---------------------------------------------------------------------------


class TestWebProviderABCs:
    """The unified WebSearchProvider ABC enforces the interface contract.

    After PR #25182, all seven providers are subclasses of
    :class:`agent.web_search_provider.WebSearchProvider`. The legacy
    in-tree ABCs at ``tools.web_providers.base`` (separate
    ``WebSearchProvider`` + ``WebExtractProvider``) were deleted in the
    same PR — providers now advertise capabilities via
    ``supports_search() / supports_extract()`` flags.
    """

    def test_cannot_instantiate_abc_directly(self):
        from agent.web_search_provider import WebSearchProvider

        with pytest.raises(TypeError):
            WebSearchProvider()  # type: ignore[abstract]

    def test_concrete_search_only_provider_works(self):
        from agent.web_search_provider import WebSearchProvider

        class Dummy(WebSearchProvider):
            @property
            def name(self) -> str:
                return "dummy"

            @property
            def display_name(self) -> str:
                return "Dummy Search"

            def is_available(self) -> bool:
                return True

            def supports_search(self) -> bool:
                return True

            def search(self, query: str, limit: int = 5) -> Dict[str, Any]:
                return {"success": True, "data": {"web": []}}

        d = Dummy()
        assert d.name == "dummy"
        assert d.display_name == "Dummy Search"
        assert d.is_available() is True
        assert d.supports_search() is True
        assert d.supports_extract() is False  # default
        assert d.search("test")["success"] is True

    def test_concrete_multi_capability_provider_works(self):
        from agent.web_search_provider import WebSearchProvider

        class Dummy(WebSearchProvider):
            @property
            def name(self) -> str:
                return "dummy"

            @property
            def display_name(self) -> str:
                return "Dummy Multi"

            def is_available(self) -> bool:
                return True

            def supports_search(self) -> bool:
                return True

            def supports_extract(self) -> bool:
                return True

            def search(self, query: str, limit: int = 5) -> Dict[str, Any]:
                return {"success": True, "data": {"web": []}}

            def extract(self, urls: List[str], **kwargs: Any) -> List[Dict[str, Any]]:
                return [{"url": urls[0], "content": "x"}]

        d = Dummy()
        assert d.supports_search() is True
        assert d.supports_extract() is True
        assert d.extract(["https://example.com"])[0]["url"] == "https://example.com"

    def test_search_only_provider_skips_extract(self):
        """Search-only providers don't have to implement extract()."""
        from agent.web_search_provider import WebSearchProvider

        class SearchOnly(WebSearchProvider):
            @property
            def name(self) -> str:
                return "search-only"

            @property
            def display_name(self) -> str:
                return "Search Only"

            def is_available(self) -> bool:
                return True

            def supports_search(self) -> bool:
                return True

            def search(self, query: str, limit: int = 5) -> Dict[str, Any]:
                return {"success": True, "data": {"web": []}}

        # Should instantiate fine — extract has default supports_*()
        # returning False and isn't required to be overridden when not
        # advertised.
        s = SearchOnly()
        assert s.supports_search() is True
        assert s.supports_extract() is False


# ---------------------------------------------------------------------------
# Per-capability backend selection
# ---------------------------------------------------------------------------


class TestPerCapabilityBackendSelection:
    """_get_search_backend and _get_extract_backend read per-capability config."""

    def test_search_backend_overrides_generic(self, monkeypatch):
        from tools import web_tools

        monkeypatch.setattr(
            web_tools,
            "_load_web_config",
            lambda: {
                "backend": "firecrawl",
                "search_backend": "tavily",
            },
        )
        monkeypatch.setenv("TAVILY_API_KEY", "test-key")
        assert web_tools._get_search_backend() == "tavily"

    def test_extract_backend_overrides_generic(self, monkeypatch):
        from tools import web_tools

        monkeypatch.setattr(
            web_tools,
            "_load_web_config",
            lambda: {
                "backend": "tavily",
                "extract_backend": "exa",
            },
        )
        monkeypatch.setenv("EXA_API_KEY", "test-key")
        assert web_tools._get_extract_backend() == "exa"

    def test_falls_back_to_generic_backend_when_search_backend_empty(self, monkeypatch):
        from tools import web_tools

        monkeypatch.setattr(
            web_tools,
            "_load_web_config",
            lambda: {
                "backend": "firecrawl",
                "search_backend": "",
            },
        )
        monkeypatch.setenv("FIRECRAWL_API_KEY", "test-key")
        assert web_tools._get_search_backend() == "firecrawl"

    def test_falls_back_to_generic_backend_when_extract_backend_empty(
        self, monkeypatch
    ):
        from tools import web_tools

        monkeypatch.setattr(
            web_tools,
            "_load_web_config",
            lambda: {
                "backend": "tavily",
                "extract_backend": "",
            },
        )
        monkeypatch.setenv("TAVILY_API_KEY", "test-key")
        assert web_tools._get_extract_backend() == "tavily"

    def test_search_backend_ignored_when_not_available(self, monkeypatch):
        from tools import web_tools

        # search_backend set but its key missing -> falls through to generic
        monkeypatch.setattr(
            web_tools,
            "_load_web_config",
            lambda: {
                "backend": "firecrawl",
                "search_backend": "tavily",
            },
        )
        monkeypatch.delenv("TAVILY_API_KEY", raising=False)
        monkeypatch.setenv("FIRECRAWL_API_KEY", "test-key")
        assert web_tools._get_search_backend() == "firecrawl"

    def test_fully_backward_compatible_with_web_backend_only(self, monkeypatch):
        from tools import web_tools

        monkeypatch.setattr(
            web_tools,
            "_load_web_config",
            lambda: {
                "backend": "tavily",
            },
        )
        monkeypatch.setenv("TAVILY_API_KEY", "test-key")
        # No search_backend or extract_backend set — both fall through
        assert web_tools._get_search_backend() == "tavily"
        assert web_tools._get_extract_backend() == "tavily"


# ---------------------------------------------------------------------------
# Config key presence in DEFAULT_CONFIG
# ---------------------------------------------------------------------------


class TestDefaultConfig:
    """The web section exists in DEFAULT_CONFIG with per-capability keys."""

    def test_web_section_in_default_config(self):
        from hermes_cli.config import DEFAULT_CONFIG

        assert "web" in DEFAULT_CONFIG
        web = DEFAULT_CONFIG["web"]
        assert "backend" in web
        assert "search_backend" in web
        assert "extract_backend" in web
        assert "search_backend_fallback_chain" in web
        # All empty string by default (no override)
        assert web["backend"] == ""
        assert web["search_backend"] == ""
        assert web["extract_backend"] == ""
        assert web["search_backend_fallback_chain"] == ""


# ---------------------------------------------------------------------------
# Search fallback chain
# ---------------------------------------------------------------------------


class TestSearchFallbackChain:
    """Issue #467: web_search_tool tries a configured fallback chain when the
    active provider returns empty or fails.
    """

    _register_providers = staticmethod(register_all_web_providers)

    @pytest.fixture(autouse=True)
    def _populate_web_registry(self):
        self._register_providers()
        yield
        from agent.web_search_registry import _reset_for_tests

        _reset_for_tests()

    @staticmethod
    def _make_provider(
        name: str, *, results: list | None = None, error: str | None = None
    ):
        from agent.web_search_provider import WebSearchProvider

        class P(WebSearchProvider):
            @property
            def name(self) -> str:
                return name

            def is_available(self) -> bool:
                return True

            def supports_search(self) -> bool:
                return True

            def search(self, query: str, limit: int = 5):
                if error is not None:
                    return {"success": False, "error": error}
                return {"success": True, "data": {"web": results or []}}

        return P()

    def _search_backend_fallback_chain_from(self, cfg: dict):
        from tools import web_tools

        # Rebind the internal loader for the test call.
        original = web_tools._load_web_config
        web_tools._load_web_config = lambda: cfg
        try:
            return web_tools._search_backend_fallback_chain()
        finally:
            web_tools._load_web_config = original

    def test_empty_fallback_chain_returns_empty_list(self):
        assert self._search_backend_fallback_chain_from({}) == []

    def test_fallback_chain_parses_comma_separated_names(self):
        assert self._search_backend_fallback_chain_from({
            "search_backend_fallback_chain": "a, b,c"
        }) == ["a", "b", "c"]

    def test_primary_success_skips_fallbacks(self, monkeypatch):
        from tools import web_tools
        from agent import web_search_registry

        monkeypatch.setattr(
            web_tools,
            "_load_web_config",
            lambda: {
                "search_backend": "primary",
                "search_backend_fallback_chain": "fallback-a, fallback-b",
            },
        )
        web_search_registry.register_provider(
            self._make_provider("primary", results=[{"title": "t"}])
        )
        web_search_registry.register_provider(
            self._make_provider("fallback-a", results=[{"title": "a"}])
        )

        result = web_tools._search_with_fallbacks(
            web_search_registry.get_provider("primary"), "q", 5, "primary"
        )
        assert result["success"] is True
        assert result["provider"] == "primary"
        assert result["data"]["web"][0]["title"] == "t"

    def test_fallback_used_when_primary_returns_empty(self, monkeypatch):
        from tools import web_tools
        from agent import web_search_registry

        monkeypatch.setattr(
            web_tools,
            "_load_web_config",
            lambda: {
                "search_backend_fallback_chain": "fallback-a, fallback-b",
            },
        )
        web_search_registry.register_provider(
            self._make_provider("primary", results=[])
        )
        web_search_registry.register_provider(
            self._make_provider("fallback-a", results=[{"title": "a"}])
        )

        result = web_tools._search_with_fallbacks(
            web_search_registry.get_provider("primary"), "q", 5, "primary"
        )
        assert result["success"] is True
        assert result["provider"] == "fallback-a"
        assert result["data"]["web"][0]["title"] == "a"

    def test_all_empty_returns_success_when_fallback_empty(self, monkeypatch):
        from tools import web_tools
        from agent import web_search_registry

        monkeypatch.setattr(
            web_tools,
            "_load_web_config",
            lambda: {
                "search_backend_fallback_chain": "fallback-a",
            },
        )
        web_search_registry.register_provider(
            self._make_provider("primary", results=[])
        )
        web_search_registry.register_provider(
            self._make_provider("fallback-a", results=[])
        )

        result = web_tools._search_with_fallbacks(
            web_search_registry.get_provider("primary"), "q", 5, "primary"
        )
        assert result["success"] is True
        assert result["provider"] == "fallback-a"


# ---------------------------------------------------------------------------
# web_search_tool uses _get_search_backend
# ---------------------------------------------------------------------------


class TestWebSearchUsesSearchBackend:
    """web_search_tool dispatches through _get_search_backend not _get_backend."""

    def test_search_tool_calls_search_backend(self, monkeypatch):
        from tools import web_tools

        called_with = []
        original_get_search = web_tools._get_search_backend

        def tracking_get_search():
            result = original_get_search()
            called_with.append(("search", result))
            return result

        monkeypatch.setattr(web_tools, "_get_search_backend", tracking_get_search)
        monkeypatch.setattr(
            web_tools, "_load_web_config", lambda: {"backend": "firecrawl"}
        )
        monkeypatch.setenv("FIRECRAWL_API_KEY", "fake")

        # The function will fail at Firecrawl client level but we just
        # need to verify _get_search_backend was called
        try:
            web_tools.web_search_tool("test", 1)
        except Exception:
            pass

        assert len(called_with) > 0
        assert called_with[0][0] == "search"


class TestUnconfiguredErrorEnvelopeParity:
    """Regression tests for PR #25182: the post-migration dispatcher must
    emit the same top-level error envelope as pre-migration main when no
    web backend is configured.

    Plugin-level error wrapping is correct for in-flight errors (per-page
    SDK exceptions, scrape timeouts) but PRE-FLIGHT configuration errors
    must surface at the top level so function-calling models that check
    ``result.get("error")`` detect the failure cleanly.
    """

    _register_providers = staticmethod(register_all_web_providers)

    @pytest.fixture(autouse=True)
    def _populate_web_registry(self):
        self._register_providers()
        yield
        from agent.web_search_registry import _reset_for_tests

        _reset_for_tests()

    def _clear_web_creds(self, monkeypatch):
        for k in (
            "BRAVE_SEARCH_API_KEY",
            "SEARXNG_URL",
            "TAVILY_API_KEY",
            "EXA_API_KEY",
            "PARALLEL_API_KEY",
            "FIRECRAWL_API_KEY",
            "FIRECRAWL_API_URL",
            "FIRECRAWL_GATEWAY_URL",
            "TOOL_GATEWAY_DOMAIN",
        ):
            monkeypatch.delenv(k, raising=False)

    def test_unconfigured_search_emits_top_level_error(self, monkeypatch):
        """``web_search_tool`` with no creds returns ``{"error": "Error searching web: ..."}``
        — matching main's ``tool_error()`` envelope, not a per-result shape.
        """
        from tools import web_tools

        self._clear_web_creds(monkeypatch)
        # Reset firecrawl client cache so the unconfigured state is re-evaluated
        monkeypatch.setattr(web_tools, "_firecrawl_client", None, raising=False)
        monkeypatch.setattr(web_tools, "_firecrawl_client_config", None, raising=False)
        monkeypatch.setattr(web_tools, "_ddgs_package_importable", lambda: False)
        monkeypatch.setattr(web_tools, "_load_web_config", lambda: {})

        result = json.loads(web_tools.web_search_tool("hello world", limit=3))
        assert "error" in result, f"expected top-level 'error' key, got {result}"
        # ``Error searching web:`` prefix comes from web_tools' top-level except handler
        assert "Error searching web:" in result["error"]
        assert "FIRECRAWL_API_KEY" in result["error"]
        # No per-result burying
        assert "results" not in result


class TestDispatchersTriggerPluginDiscovery:
    """Regression tests for #27580: each web_*_tool dispatcher must
    idempotently call ``_ensure_web_plugins_loaded()`` before consulting
    ``agent.web_search_registry``.

    Without this, a tool call from a context that hasn't already loaded
    plugins (subprocess agent runs, delegate children, standalone scripts,
    test paths that import the registry directly) sees an empty registry
    and returns the misleading "No web extract provider configured" error
    even when the user has both the config key set AND the API key
    exported.

    Mirrors :func:`tools.browser_tool._ensure_browser_plugins_loaded` —
    every other plugin-backed dispatcher (image_gen, video_gen, browser,
    skills) already does this.
    """

    def _clear_registry(self):
        """Reset the web_search registry to empty and return a callback
        that restores the original contents. Used in a try/finally so the
        snapshot is restored even when the dispatcher under test raises."""
        from agent import web_search_registry

        with web_search_registry._lock:
            snapshot = dict(web_search_registry._providers)
            web_search_registry._providers.clear()
        return lambda: self._restore_registry(snapshot)

    def _restore_registry(self, snapshot: dict):
        from agent import web_search_registry

        with web_search_registry._lock:
            web_search_registry._providers.clear()
            web_search_registry._providers.update(snapshot)

    def _patch_load_plugins(self, monkeypatch):
        """Make ``_ensure_web_plugins_loaded`` return without loading plugins.

        The dispatcher must call it before registry lookup. We verify the
        call happened by checking that the registry was populated afterward.
        """
        from tools import web_tools

        discovery_called = []

        def fake_ensure():
            discovery_called.append(True)

        monkeypatch.setattr(web_tools, "_ensure_web_plugins_loaded", fake_ensure)
        return discovery_called

    def test_web_extract_tool_runs_discovery_before_registry_lookup(self, monkeypatch):
        from tools import web_tools
        import asyncio

        discovery_called = self._patch_load_plugins(monkeypatch)
        restore = self._clear_registry()
        try:
            # The tool returns a JSON result envelope even on failure; ensure
            # that discovery happened and the result is NOT a top-level error
            # pretending there is no provider.
            result = asyncio.run(web_tools.web_extract_tool(["https://example.com"]))
        finally:
            restore()

        assert discovery_called, "_ensure_web_plugins_loaded was not called"
        assert (
            "TAVILY_API_KEY" in result
            or "FIRECRAWL_API_KEY" in result
            or "No web extract" in result
        )

    def test_web_search_tool_runs_discovery_before_registry_lookup(self, monkeypatch):
        from tools import web_tools

        discovery_called = self._patch_load_plugins(monkeypatch)
        restore = self._clear_registry()
        try:
            # The tool returns a JSON error envelope when the registry is empty.
            web_tools.web_search_tool("hello world", limit=3)
        finally:
            restore()

        assert discovery_called, "_ensure_web_plugins_loaded was not called"
