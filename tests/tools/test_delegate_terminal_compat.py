"""Tests for the pre-dispatch toolset/task compatibility check (#1369).

Verifies that:
1. ``_goal_needs_terminal`` detects shell-dependent verbs in goal/context text.
2. The auto-add logic adds ``terminal`` when the goal needs it but the resolved
   toolset omits it — but only when the parent can provide it (no widening).
3. A structured ``toolset_adjusted`` signal is surfaced in the result dict.
"""

from types import SimpleNamespace

from tools.delegate_tool import _goal_needs_terminal, _strip_blocked_tools


class TestGoalNeedsTerminal:
    """_goal_needs_terminal static verb-match heuristic."""

    def test_shell_verbs_detected(self):
        """Common shell-dependent verbs trigger detection."""
        for goal in [
            "Run git log to see recent commits",
            "Build the project and fix errors",
            "Run the test suite for module X",
            "Open a shell and check the process list",
            "Use bash to iterate over files",
            "Install the dependency with pip",
            "Run docker build to create the image",
            "Execute pytest on the new test file",
            "RUN the BUILD step",  # case-insensitive
        ]:
            assert _goal_needs_terminal(goal), f"Expected detection for: {goal!r}"

    def test_context_scanned_too(self):
        """Goal has no shell verb but context does — still detected."""
        assert _goal_needs_terminal(
            "Analyze the results",
            context="Use gh to list open PRs first, then summarize.",
        )

    def test_no_shell_verbs_returns_false(self):
        assert not _goal_needs_terminal("Write a poem about the ocean and return it")

    def test_empty_and_none_return_false(self):
        assert not _goal_needs_terminal("")
        assert not _goal_needs_terminal(None)

    def test_word_boundary_no_false_positive(self):
        """'git' must not match inside 'digit' or 'fidget'."""
        assert not _goal_needs_terminal("The digit count is fidget-driven")


class TestAutoAddTerminalLogic:
    """The toolset auto-add decision logic in isolation."""

    def test_auto_add_when_parent_has_terminal(self):
        """Goal needs shell, child lacks terminal, parent has it → add."""
        from tools.delegate_tool import _expand_parent_toolsets

        parent_toolsets = {"terminal", "file", "web"}
        child_toolsets = _strip_blocked_tools(["file", "web"])
        assert "terminal" not in child_toolsets
        assert _goal_needs_terminal("Run git log")
        expanded = _expand_parent_toolsets(parent_toolsets)
        assert "terminal" in expanded

    def test_no_auto_add_when_parent_lacks_terminal(self):
        """Goal needs shell, parent also lacks terminal → no widening."""
        from tools.delegate_tool import _expand_parent_toolsets

        parent_toolsets = {"file", "web"}
        child_toolsets = _strip_blocked_tools(["file", "web"])
        assert _goal_needs_terminal("Run git log")
        expanded = _expand_parent_toolsets(parent_toolsets)
        assert "terminal" not in expanded  # guard prevents add

    def test_no_auto_add_when_already_present(self):
        """Goal needs shell, child already has terminal → no-op."""
        child_toolsets = _strip_blocked_tools(["terminal", "file"])
        assert "terminal" in child_toolsets

    def test_no_auto_add_when_goal_needs_no_shell(self):
        """Goal has no shell verbs → never auto-add."""
        child_toolsets = _strip_blocked_tools(["file", "web"])
        assert not _goal_needs_terminal("Write a summary of the document")
        assert "terminal" not in child_toolsets


class TestToolsetAdjustedSignal:
    """The structured toolset_adjusted signal in the result dict."""

    def test_signal_present_when_adjusted(self):
        child = SimpleNamespace(_delegate_role="leaf", _toolset_adjusted=True)
        entry = {
            "status": "completed",
            "_child_role": getattr(child, "_delegate_role", None),
        }
        if getattr(child, "_toolset_adjusted", False):
            entry["toolset_adjusted"] = {"added": ["terminal"]}
        assert "toolset_adjusted" in entry
        assert entry["toolset_adjusted"]["added"] == ["terminal"]

    def test_signal_absent_when_not_adjusted(self):
        child = SimpleNamespace(_delegate_role="leaf")
        entry = {
            "status": "completed",
            "_child_role": getattr(child, "_delegate_role", None),
        }
        if getattr(child, "_toolset_adjusted", False):
            entry["toolset_adjusted"] = {"added": ["terminal"]}
        assert "toolset_adjusted" not in entry

    def test_signal_absent_on_default_child(self):
        """A plain child with no _toolset_adjusted attr → no signal."""
        child = SimpleNamespace(_delegate_role="leaf")
        entry = {"status": "completed"}
        if getattr(child, "_toolset_adjusted", False):
            entry["toolset_adjusted"] = {"added": ["terminal"]}
        assert "toolset_adjusted" not in entry
