# -*- coding: utf-8 -*-
"""Executable-seam integration tests for #1137 listwise reranking in tool_search.

Drive the real ``dispatch_tool_search`` handler to prove the listwise reranker
is wired into the retrieval path and inert unless ``skill_routing.listwise_rerank``
is enabled.
"""

from unittest.mock import patch

from tools.tool_search import CatalogEntry, ToolSearchConfig, dispatch_tool_search


def _entry(name, description):
    return CatalogEntry(
        name=name,
        description=description,
        schema={"type": "function", "function": {"name": name, "description": description}},
        source="mcp",
        source_name="mcp-test",
    )


def _hits_bm25_suboptimal():
    # BM25 returns the weak match first, the strong one second.
    return [
        _entry("send_email", "send an email message to a recipient"),
        _entry("read_file", "read a file from disk"),
    ]


def test_tool_search_default_off_preserves_bm25_order():
    cfg = ToolSearchConfig.from_raw(None)
    with (
        patch("tools.tool_search.search_catalog", return_value=_hits_bm25_suboptimal()),
        patch("hermes_cli.config.load_config", return_value={}),
    ):
        out = dispatch_tool_search({"query": "read a file"}, current_tool_defs=[], config=cfg)
    import json

    matches = json.loads(out)["matches"]
    assert [m["name"] for m in matches] == ["send_email", "read_file"]


def test_tool_search_listwise_rerank_promotes_best_match():
    cfg = ToolSearchConfig.from_raw(None)
    with (
        patch("tools.tool_search.search_catalog", return_value=_hits_bm25_suboptimal()),
        patch("hermes_cli.config.load_config", return_value={"skill_routing": {"listwise_rerank": True}}),
    ):
        out = dispatch_tool_search({"query": "read a file"}, current_tool_defs=[], config=cfg)
    import json

    matches = json.loads(out)["matches"]
    # reranked: the strong lexical match rises to the top
    assert matches[0]["name"] == "read_file"
    # strict reordering — same set of tools returned
    assert {m["name"] for m in matches} == {"send_email", "read_file"}


def test_tool_search_rerank_failure_degrades_gracefully():
    cfg = ToolSearchConfig.from_raw(None)
    with (
        patch("tools.tool_search.search_catalog", return_value=_hits_bm25_suboptimal()),
        patch("agent.skill_routing.maybe_rerank_hits", side_effect=RuntimeError("boom")),
        patch("hermes_cli.config.load_config", return_value={"skill_routing": {"listwise_rerank": True}}),
    ):
        out = dispatch_tool_search({"query": "read a file"}, current_tool_defs=[], config=cfg)
    import json

    # the try/except in dispatch keeps the original hits on any rerank error
    matches = json.loads(out)["matches"]
    assert [m["name"] for m in matches] == ["send_email", "read_file"]
