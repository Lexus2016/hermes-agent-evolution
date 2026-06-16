"""Tests for tools/browser_navigate_fallback.py (#234/#213)."""

import tools.browser_navigate_fallback as bnf


class TestClassify:
    def test_cdp_unavailable(self):
        assert bnf.classify_navigation_error("CDP command timed out: Page.navigate") in (
            "cdp_unavailable", "navigation_timeout")  # both are valid; timeout wins by order

    def test_pure_cdp(self):
        assert bnf.classify_navigation_error("Could not connect to Chrome backend") == "cdp_unavailable"

    def test_timeout(self):
        assert bnf.classify_navigation_error("navigation timed out after 60s") == "navigation_timeout"

    def test_tool_not_present(self):
        assert bnf.classify_navigation_error("Tool does not exist. Available tools: open, click") == "tool_not_present"

    def test_dom_error(self):
        assert bnf.classify_navigation_error("Could not compute box model") == "dom_error"

    def test_generic(self):
        assert bnf.classify_navigation_error("something weird happened") == "navigation_error"

    def test_empty(self):
        assert bnf.classify_navigation_error(None) == "navigation_error"
        assert bnf.classify_navigation_error("") == "navigation_error"


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
        assert r["failure_class"] == "cdp_unavailable"
        assert r["fallback_error"] == "extract boom"
        assert "web_search" in r["recovery"] or "extracted" in r["recovery"]

    def test_cap_message_after_threshold(self, monkeypatch):
        monkeypatch.setattr(bnf, "web_extract_fallback", lambda url: (None, "no"))
        for _ in range(bnf.MAX_NAV_FAILURES - 1):
            bnf.build_navigation_failure("https://x", "timeout")
        r = bnf.build_navigation_failure("https://x", "timeout")
        assert r["nav_failures_for_url"] == bnf.MAX_NAV_FAILURES
        assert "cap" in r["recovery"].lower() and "STOP" in r["recovery"]
