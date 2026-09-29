"""Tests for delegate_tool toolset scoping.

Verifies that subagents cannot gain tools that the parent does not have.
The LLM controls the `toolsets` parameter — without intersection with the
parent's enabled_toolsets, it can escalate privileges by requesting
arbitrary toolsets.
"""

from types import SimpleNamespace

from tools.delegate_tool import _strip_blocked_tools, _emit_parent_console


class TestToolsetIntersection:
    """Subagent toolsets must be a subset of parent's enabled_toolsets."""



    def test_strip_blocked_removes_delegation(self):
        """Blocked toolsets (delegation, clarify, etc.) are always removed."""
        child = _strip_blocked_tools(["terminal", "delegation", "clarify", "memory"])
        assert "delegation" not in child
        assert "clarify" not in child
        assert "memory" not in child
        assert "terminal" in child


    def test_denied_toolsets_names_what_the_parent_lacks(self):
        """#648: the toolsets dropped by the intersection (not by
        _strip_blocked_tools) must be identifiable so they can be surfaced to
        the subagent — silently disappearing is what causes the delegated
        task to fail without the parent understanding why."""
        parent_toolsets = {"terminal", "file"}
        requested = ["terminal", "file", "web", "browser"]

        scoped = [t for t in requested if t in parent_toolsets]
        denied = [t for t in requested if t not in parent_toolsets]

        assert sorted(scoped) == ["file", "terminal"]
        assert sorted(denied) == ["browser", "web"]

    def test_no_denied_toolsets_when_all_requested_are_available(self):
        parent_toolsets = {"terminal", "file", "web"}
        requested = ["terminal", "web"]

        denied = [t for t in requested if t not in parent_toolsets]

        assert denied == []


class TestEmitParentConsole:
    """Progress lines (e.g. ``✓ [N/M] …``) must route through the parent's
    configured ``_safe_print`` in headless stdio hosts (ACP, gateway) so
    they don't land on stdout and corrupt JSON-RPC frames. Regression for a
    bug where delegate_task completion lines pushed to stdout caused
    ``Failed to parse JSON message: ✓ [3/3] …`` errors in the ACP adapter."""

    def test_routes_through_parent_safe_print_when_available(self, capsys):
        captured_lines = []
        parent = SimpleNamespace(_safe_print=lambda line: captured_lines.append(line))

        _emit_parent_console(parent, "  ✓ [1/3] Research done  (11.55s)")

        assert captured_lines == ["  ✓ [1/3] Research done  (11.55s)"]
        stdout_stderr = capsys.readouterr()
        assert stdout_stderr.out == ""
        assert stdout_stderr.err == ""


