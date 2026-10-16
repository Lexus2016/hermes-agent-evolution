"""Cache + degraded-discovery for the deferred-tool search catalog (issue #140).

``dispatch_tool_search`` rebuilt the full catalog (classify + tokenize + stem
+ BM25 index) on every call; under load that per-call cost was the dominant
contributor to the tool_search timeout failures (~0.71 per session). These
tests pin the signature-keyed catalog cache and the degraded-discovery path
that surfaces the tool list instead of failing silently.
"""

import json

import pytest


def _td(name, desc="Deferred capability."):
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": desc,
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    }


def _register(name, toolset="mcp-test", desc="Deferred capability."):
    from tools.registry import registry

    registry.register(
        name=name,
        handler=lambda args, **kw: json.dumps({"ok": True}),
        schema=_td(name, desc),
        toolset=toolset,
    )
    return _td(name, desc)


@pytest.fixture(autouse=True)
def _clean_cache():
    from tools.tool_search import clear_search_catalog_cache

    clear_search_catalog_cache()
    yield
    clear_search_catalog_cache()


@pytest.fixture
def defs():
    return [
        _register("create_issue", desc="File an issue on a repository."),
        _register("send_slack", desc="Post a message to a Slack channel."),
    ]


class TestSearchCatalogCache:
    def test_catalog_built_once_per_signature(self, defs, monkeypatch):
        from tools.tool_search import ToolSearchConfig, build_catalog, dispatch_tool_search

        calls = {"n": 0}
        real_build = build_catalog

        def counting(tool_defs):
            calls["n"] += 1
            return real_build(tool_defs)

        monkeypatch.setattr("tools.tool_search.build_catalog", counting)
        cfg = ToolSearchConfig.from_raw({})
        for _ in range(5):
            dispatch_tool_search(
                {"queries": ["issue"]}, current_tool_defs=defs, config=cfg
            )
        assert calls["n"] == 1

    def test_invalidates_on_toolset_change(self, defs, monkeypatch):
        from tools.tool_search import ToolSearchConfig, build_catalog, dispatch_tool_search

        calls = {"n": 0}
        real_build = build_catalog

        def counting(tool_defs):
            calls["n"] += 1
            return real_build(tool_defs)

        monkeypatch.setattr("tools.tool_search.build_catalog", counting)
        cfg = ToolSearchConfig.from_raw({})
        dispatch_tool_search(
            {"queries": ["issue"]}, current_tool_defs=defs, config=cfg
        )
        assert calls["n"] == 1

        # A new tool joins the session -> new signature -> rebuild.
        grown = defs + [_register("post_tweet", desc="Post to a Twitter account.")]
        dispatch_tool_search(
            {"queries": ["tweet"]}, current_tool_defs=grown, config=cfg
        )
        assert calls["n"] == 2

    def test_invalidates_on_config_change(self, defs, monkeypatch):
        from tools.tool_search import ToolSearchConfig, build_catalog, dispatch_tool_search

        calls = {"n": 0}
        real_build = build_catalog

        def counting(tool_defs):
            calls["n"] += 1
            return real_build(tool_defs)

        monkeypatch.setattr("tools.tool_search.build_catalog", counting)
        cfg_a = ToolSearchConfig.from_raw({})
        cfg_b = ToolSearchConfig.from_raw({"defer_core_toolsets": ["mcp-test"]})
        dispatch_tool_search(
            {"queries": ["issue"]}, current_tool_defs=defs, config=cfg_a
        )
        dispatch_tool_search(
            {"queries": ["issue"]}, current_tool_defs=defs, config=cfg_b
        )
        assert calls["n"] == 2

    def test_cache_hit_preserves_results(self, defs):
        from tools.tool_search import ToolSearchConfig, dispatch_tool_search

        cfg = ToolSearchConfig.from_raw({})
        first = json.loads(
            dispatch_tool_search(
                {"queries": ["issue"]}, current_tool_defs=defs, config=cfg
            )
        )
        second = json.loads(
            dispatch_tool_search(
                {"queries": ["issue"]}, current_tool_defs=defs, config=cfg
            )
        )
        assert first == second
        assert "create_issue" in first["results"][0]["matches"]

    def test_clear_search_catalog_cache(self, defs, monkeypatch):
        from tools.tool_search import (
            ToolSearchConfig,
            build_catalog,
            clear_search_catalog_cache,
            dispatch_tool_search,
        )

        calls = {"n": 0}
        real_build = build_catalog

        def counting(tool_defs):
            calls["n"] += 1
            return real_build(tool_defs)

        monkeypatch.setattr("tools.tool_search.build_catalog", counting)
        cfg = ToolSearchConfig.from_raw({})
        dispatch_tool_search(
            {"queries": ["issue"]}, current_tool_defs=defs, config=cfg
        )
        clear_search_catalog_cache()
        dispatch_tool_search(
            {"queries": ["issue"]}, current_tool_defs=defs, config=cfg
        )
        assert calls["n"] == 2


class TestDegradedDiscovery:
    def test_build_failure_surfaces_tool_list(self, defs, monkeypatch):
        from tools.tool_search import ToolSearchConfig, build_catalog, dispatch_tool_search

        def boom(tool_defs):
            raise RuntimeError("index corrupt")

        monkeypatch.setattr("tools.tool_search.build_catalog", boom)
        resp = json.loads(
            dispatch_tool_search(
                {"queries": ["issue"]},
                current_tool_defs=defs,
                config=ToolSearchConfig.from_raw({}),
            )
        )
        assert resp["degraded"] is True
        assert "catalog build failed" in resp["degraded_reason"]
        # The tool list still surfaces instead of an empty catalog.
        assert resp["total_available"] >= 2
        assert "create_issue" in resp["results"][0]["matches"]
        # Bridge tools never leak into the degraded listing.
        assert "tool_call" not in resp["results"][0]["matches"]

    def test_build_failure_multiquery(self, defs, monkeypatch):
        from tools.tool_search import ToolSearchConfig, build_catalog, dispatch_tool_search

        def boom(tool_defs):
            raise ValueError("schema parse failed")

        monkeypatch.setattr("tools.tool_search.build_catalog", boom)
        resp = json.loads(
            dispatch_tool_search(
                {"queries": ["issue", "slack"]},
                current_tool_defs=defs,
                config=ToolSearchConfig.from_raw({}),
            )
        )
        assert resp["degraded"] is True
        by_query = {r["query"]: r["matches"] for r in resp["results"]}
        assert "create_issue" in by_query["issue"]
        assert "send_slack" in by_query["slack"]


def test_same_name_changed_schema_is_not_cached(monkeypatch):
    import json
    from tools import tool_search as ts
    ts.clear_describe_cache()
    first = [{"type": "function", "function": {"name": "mcp_demo_lookup", "description": "original", "parameters": {"type": "object"}}}]
    second = [{"type": "function", "function": {"name": "mcp_demo_lookup", "description": "updated", "parameters": {"type": "object"}}}]
    monkeypatch.setattr(ts, "is_deferrable_tool_name", lambda *args, **kw: True)
    config = ts.ToolSearchConfig.from_raw({})
    a = json.loads(ts.dispatch_tool_describe({"name": "mcp_demo_lookup"}, current_tool_defs=first, config=config))
    b = json.loads(ts.dispatch_tool_describe({"name": "mcp_demo_lookup"}, current_tool_defs=second, config=config))
    assert a["description"] == "original"
    assert b["description"] == "updated"
    ts.clear_describe_cache()
