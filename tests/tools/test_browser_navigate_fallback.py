"""Tests for tools/browser_navigate_fallback.py (#234/#213/#745).

Since #745 the browser failure classes are the SHARED ``agent/tool_diagnostics``
categories (no parallel browser-only taxonomy): CDP/backend down -> provider_dead,
navigation timeout -> timeout, absent tool set -> missing_command, DOM read
failure / generic -> runtime_error.
"""

import agent.tool_diagnostics as td
import tools.browser_navigate_fallback as bnf


# Every class the browser classifier can return must be a real tool_diagnostics
# category — this is what "no parallel taxonomy" means concretely.
_SHARED_CATEGORIES = {cat for _pat, cat, _hint in td._RULES}


class TestClassify:
    def test_cdp_timed_out_is_provider_dead_or_timeout(self):
        # "CDP ... timed out" matches both the CDP (provider_dead) and timeout
        # signals; the CDP rule is ordered first, so provider_dead wins.
        assert bnf.classify_browser_error("CDP command timed out: Page.navigate") in (
            "provider_dead", "timeout"
        )

    def test_pure_cdp_is_provider_dead(self):
        assert bnf.classify_browser_error("Could not connect to Chrome backend") == "provider_dead"

    def test_timeout(self):
        assert bnf.classify_browser_error("navigation timed out after 60s") == "timeout"

    def test_tool_not_present_is_missing_command(self):
        assert bnf.classify_browser_error(
            "Tool does not exist. Available tools: open, click"
        ) == "missing_command"

    def test_dom_error_is_runtime_error(self):
        assert bnf.classify_browser_error("Could not compute box model") == "runtime_error"

    def test_generic_is_runtime_error(self):
        assert bnf.classify_browser_error("something weird happened") == "runtime_error"

    def test_empty_is_runtime_error(self):
        assert bnf.classify_browser_error(None) == "runtime_error"
        assert bnf.classify_browser_error("") == "runtime_error"

    def test_navigation_alias_matches_browser_error(self):
        # Back-compat alias returns the same shared category.
        assert bnf.classify_navigation_error("navigation timed out") == "timeout"

    def test_all_outputs_are_shared_categories(self):
        samples = [
            "Could not connect to Chrome backend",
            "navigation timed out after 60s",
            "Tool does not exist. Available tools: open",
            "Could not compute box model",
            "something weird happened",
            "permission denied writing profile",
            None,
            "",
        ]
        for s in samples:
            assert bnf.classify_browser_error(s) in _SHARED_CATEGORIES


class TestFailureCounter:
    def setup_method(self):
        bnf._nav_failures.clear()

    def test_increments_and_resets(self):
        assert bnf.record_nav_failure("u") == 1
        assert bnf.record_nav_failure("u") == 2
        bnf.reset_nav_failures("u")
        assert bnf.record_nav_failure("u") == 1

    def test_independent_urls(self):
        bnf.record_nav_failure("a")
        assert bnf.record_nav_failure("b") == 1


class TestBuildNavigationFailure:
    def setup_method(self):
        bnf._nav_failures.clear()

    def test_fallback_content_returned(self, monkeypatch):
        monkeypatch.setattr(bnf, "web_extract_fallback", lambda url: ("# Page text", None))
        r = bnf.build_navigation_failure("https://x", "CDP command timed out")
        assert r["success"] is False
        assert r["fallback_used"] == "web_extract"
        assert r["fallback_content"] == "# Page text"
        assert "do NOT re-navigate" in r["recovery"]

    def test_fallback_failed_gives_taxonomy_hint(self, monkeypatch):
        monkeypatch.setattr(bnf, "web_extract_fallback", lambda url: (None, "extract boom"))
        r = bnf.build_navigation_failure("https://x", "Could not connect to Chrome")
        assert r["fallback_used"] is None
        assert r["failure_class"] == "provider_dead"
        assert r["failure_class"] in _SHARED_CATEGORIES
        assert r["fallback_error"] == "extract boom"
        assert "web_search" in r["recovery"] or "extracted" in r["recovery"]

    def test_cap_message_after_threshold(self, monkeypatch):
        monkeypatch.setattr(bnf, "web_extract_fallback", lambda url: (None, "no"))
        for _ in range(bnf.MAX_NAV_FAILURES - 1):
            bnf.build_navigation_failure("https://x", "timeout")
        r = bnf.build_navigation_failure("https://x", "timeout")
        assert r["nav_failures_for_url"] == bnf.MAX_NAV_FAILURES
        assert "cap" in r["recovery"].lower() and "STOP" in r["recovery"]

