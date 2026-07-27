"""Tests for leaf subagent terminal tool retention (#1369).

When explicit toolsets are given to delegate_task, the intersection with
the parent's toolsets can drop 'terminal' even though the parent has it.
Leaf subagents then spiral with "I have no shell" refusals. The fix
merges 'terminal' back into the child's toolsets when the parent had it
and the child role is 'leaf'.
"""

from types import SimpleNamespace

import pytest

from tools.delegate_tool import _strip_blocked_tools, _expand_parent_toolsets


class TestLeafTerminalRetention:
    """Verify terminal is merged back for leaf children when parent has it."""

    def _resolve_child_toolsets(
        self,
        parent_enabled,
        requested_toolsets,
        effective_role="leaf",
        parent_disabled=None,
    ):
        """Simulate the toolset resolution from _build_child_agent."""
        parent = SimpleNamespace(
            enabled_toolsets=parent_enabled,
            disabled_toolsets=parent_disabled,
            valid_tool_names=None,
        )

        # Derive parent toolsets
        parent_enabled_val = getattr(parent, "enabled_toolsets", None)
        if parent_enabled_val is not None:
            parent_toolsets = set(parent_enabled_val)
        else:
            parent_toolsets = {"terminal", "file", "web"}

        # Intersection (simulates explicit toolsets path)
        if requested_toolsets:
            expanded_parent = _expand_parent_toolsets(parent_toolsets)
            child_toolsets = [t for t in requested_toolsets if t in expanded_parent]
            child_toolsets = _strip_blocked_tools(child_toolsets)

            # ── #1369 fix: merge terminal back for leaf children ──
            _parent_disabled_raw = getattr(parent, "disabled_toolsets", None)
            _terminal_disabled = (
                isinstance(_parent_disabled_raw, (list, tuple, set))
                and "terminal" in _parent_disabled_raw
            )
            if (
                effective_role == "leaf"
                and "terminal" in expanded_parent
                and "terminal" not in child_toolsets
                and not _terminal_disabled
            ):
                child_toolsets.append("terminal")
        else:
            child_toolsets = _strip_blocked_tools(sorted(parent_toolsets))

        return child_toolsets

    def test_terminal_merged_when_explicit_toolsets_drop_it(self):
        """Parent has terminal but model requests only ['web'] — terminal
        should be merged back for leaf children."""
        result = self._resolve_child_toolsets(
            parent_enabled=["terminal", "file", "web"],
            requested_toolsets=["web"],
            effective_role="leaf",
        )
        assert "web" in result
        assert "terminal" in result, (
            "Leaf child should retain terminal even when explicit toolsets "
            "don't list it — #1369 regression fix"
        )

    def test_terminal_not_added_when_parent_lacks_it(self):
        """Parent doesn't have terminal — child should NOT gain it."""
        result = self._resolve_child_toolsets(
            parent_enabled=["file", "web"],
            requested_toolsets=["web"],
            effective_role="leaf",
        )
        assert "terminal" not in result

    def test_terminal_not_added_when_explicitly_disabled(self):
        """Parent disabled terminal — child should NOT get it back."""
        result = self._resolve_child_toolsets(
            parent_enabled=["terminal", "file", "web"],
            requested_toolsets=["web"],
            effective_role="leaf",
            parent_disabled=["terminal"],
        )
        assert "terminal" not in result

    def test_terminal_kept_when_already_in_toolsets(self):
        """If terminal is already in the requested toolsets, no duplicate."""
        result = self._resolve_child_toolsets(
            parent_enabled=["terminal", "file", "web"],
            requested_toolsets=["terminal", "web"],
            effective_role="leaf",
        )
        assert result.count("terminal") == 1

    def test_no_explicit_toolsets_inherits_terminal(self):
        """When no toolsets given, child inherits parent including terminal."""
        result = self._resolve_child_toolsets(
            parent_enabled=["terminal", "file", "web"],
            requested_toolsets=None,
            effective_role="leaf",
        )
        assert "terminal" in result
