"""Subagent shell capability admission regressions."""
import unittest
from unittest.mock import MagicMock


class TestChildExecCapabilityGate(unittest.TestCase):
    """#2826: shell-requiring subagents whose resolved toolset lacks
    `terminal` must be blocked VISIBLY (not dispatched to a doomed run)."""

    def _child(self, toolsets):
        c = MagicMock()
        c.enabled_toolsets = toolsets
        return c

    def test_shell_goal_without_terminal_blocked(self):
        from tools.delegate_tool import _child_blocked_no_terminal

        child = self._child(["web", "browser"])
        out = _child_blocked_no_terminal(0, "run git status in the repo", child)
        self.assertIsNotNone(out)
        assert out is not None
        self.assertEqual(out["status"], "blocked")
        self.assertEqual(out["exit_reason"], "blocked_no_terminal")
        self.assertEqual(out["api_calls"], 0)  # no doomed LLM call

    def test_shell_goal_with_terminal_proceeds(self):
        from tools.delegate_tool import _child_blocked_no_terminal

        child = self._child(["web", "terminal"])
        out = _child_blocked_no_terminal(0, "git status in the repo", child)
        self.assertIsNone(out)  # terminal present → proceed

    def test_non_shell_goal_proceeds_without_terminal(self):
        from tools.delegate_tool import _child_blocked_no_terminal

        child = self._child(["web", "browser"])
        out = _child_blocked_no_terminal(0, "summarize the page content", child)
        self.assertIsNone(out)  # no shell verbs → no gate

    def test_run_single_child_returns_blocked_before_run(self):
        """The gate is wired into _run_single_child: a child whose resolved
        toolset lacks terminal for a shell goal returns a blocked result and
        never calls run_conversation."""
        from tools.delegate_tool import _run_single_child

        child = MagicMock()
        child.enabled_toolsets = ["web", "browser"]
        child.run_conversation = MagicMock()

        out = _run_single_child(
            task_index=0,
            goal="git clone and build the repo",
            child=child,
        )
        self.assertEqual(out["status"], "blocked")
        child.run_conversation.assert_not_called()

    def test_soft_verb_file_goal_proceeds_without_terminal(self):
        """#150 regression: goals that merely mention ambiguous verbs
        (run/check/test/gh/...) must NOT be blocked for file-capable children.
        This is what deadlocked the evolution pipeline in restricted cron
        sessions: every delegate_task dispatch returned blocked_no_terminal
        even for purely file/gh-based stages."""
        from tools.delegate_tool import _child_blocked_no_terminal

        for goal in [
            "check the analysis JSON in ~/.hermes/evolution and draft issues",
            "run the analysis stage over the latest scan output",
            "test the drafted issue bodies against the template",
            "file the drafted issues with gh",
            "build the report from the merged analysis",
        ]:
            child = self._child(["file", "web"])
            out = _child_blocked_no_terminal(0, goal, child)
            self.assertIsNone(out, f"soft-verb goal must not block: {goal!r}")

    def test_hard_verb_without_terminal_still_blocked(self):
        """#150: the gate must STILL block genuinely shell-required work —
        unambiguous verbs (git/ssh/docker/pytest/...) are not covered by the
        fail-open relaxation."""
        from tools.delegate_tool import _child_blocked_no_terminal

        for goal in [
            "ssh into the host and check uptime",
            "docker build the image and push it",
            "run pytest on the new test file",
            "pip install the pinned dependencies",
            "restart the service with systemctl",
        ]:
            child = self._child(["file", "web"])
            out = _child_blocked_no_terminal(0, goal, child)
            self.assertIsNotNone(out, f"hard-verb goal must still block: {goal!r}")
            assert out is not None
            self.assertEqual(out["exit_reason"], "blocked_no_terminal")
