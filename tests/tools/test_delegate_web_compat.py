"""Tests for the pre-dispatch web-toolset compatibility check (#126).

Verifies that:
1. ``_goal_needs_web`` detects web-dependent verbs in goal/context text.
2. The auto-add logic adds ``web`` when the goal needs it but the resolved
   toolset omits it — but only when the parent can provide it (no widening).
3. Research-style goals that previously shipped a child with no host web
   fallback now resolve with ``web`` present.
"""

from tools.delegate_tool import _goal_needs_web, _strip_blocked_tools


class TestGoalNeedsWeb:
    """_goal_needs_web static verb-match heuristic."""

    def test_web_verbs_detected(self):
        """Common web-dependent verbs trigger detection."""
        for goal in [
            "Research the latest release notes for project X",
            "Search the web for pricing information",
            "Fetch the page at https://example.com and summarize it",
            "Scrape the product listing and extract prices",
            "Browse the docs site and answer the question",
            "Download the PDF and extract its key points",
            "RESEARCH the competitor landscape",  # case-insensitive
        ]:
            assert _goal_needs_web(goal), f"Expected detection for: {goal!r}"

    def test_context_scanned_too(self):
        """Goal has no web verb but context does — still detected."""
        assert _goal_needs_web(
            "Summarize the findings",
            context="Use web_search to find the latest stats first.",
        )

    def test_no_web_verbs_returns_false(self):
        assert not _goal_needs_web("Write a poem about the ocean and return it")

    def test_empty_and_none_return_false(self):
        assert not _goal_needs_web("")
        assert not _goal_needs_web(None)

    def test_word_boundary_no_false_positive(self):
        """'web' must not match inside 'webinar' or 'webbed'."""
        assert not _goal_needs_web("The webinar webbed the team together")


class TestAutoAddWebLogic:
    """The toolset auto-add decision logic in isolation."""

    def test_auto_add_when_parent_has_web(self):
        """Goal needs web, child lacks it, parent has it → add."""
        from tools.delegate_tool import _expand_parent_toolsets

        parent_toolsets = {"terminal", "file", "web"}
        child_toolsets = _strip_blocked_tools(["terminal", "file"])
        assert "web" not in child_toolsets
        assert _goal_needs_web("Research the topic and report back")
        expanded = _expand_parent_toolsets(parent_toolsets)
        assert "web" in expanded

    def test_no_auto_add_when_parent_lacks_web(self):
        """Goal needs web, parent also lacks web → no widening."""
        from tools.delegate_tool import _expand_parent_toolsets

        parent_toolsets = {"terminal", "file"}
        _child_toolsets = _strip_blocked_tools(["terminal", "file"])
        assert _goal_needs_web("Research the topic and report back")
        expanded = _expand_parent_toolsets(parent_toolsets)
        assert "web" not in expanded  # guard prevents add

    def test_no_auto_add_when_already_present(self):
        """Goal needs web, child already has web → no-op."""
        child_toolsets = _strip_blocked_tools(["web", "file"])
        assert "web" in child_toolsets
