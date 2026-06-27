"""Tests for scripts/evolution_decomposition_gate.py — decomposition gate for implementation."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

import evolution_decomposition_gate as gate  # noqa: E402


def _child(number, title="child issue"):
    return {"number": number, "title": title}


# ── Individual function tests ──────────────────────────────────────────────────


class TestCheckDecompositionGate:
    """Test the pure check_decomposition_gate() function with injected runner."""

    def _runner(self, children_list, rc=0):
        """Return a runner that returns the given children list as gh output."""

        def run(cmd):
            return rc, json.dumps(children_list), ""

        return run

    def test_passes_below_threshold(self):
        """effort < 0.4 always passes, no gh call needed."""
        result = gate.check_decomposition_gate(579, 0.3)
        assert result["passed"] is True
        assert result["child_count"] is None
        assert "no decomposition required" in result["reason"]

    def test_passes_at_threshold_with_children(self):
        """effort >= 0.4 with children found passes."""
        children = [_child(580, "slice 1"), _child(581, "slice 2")]
        result = gate.check_decomposition_gate(579, 0.5, runner=self._runner(children))
        assert result["passed"] is True
        assert result["child_count"] == 2
        assert "found 2 child issue" in result["reason"]

    def test_blocks_at_threshold_no_children(self):
        """effort >= 0.4 with no children blocks."""
        result = gate.check_decomposition_gate(579, 0.5, runner=self._runner([]))
        assert result["passed"] is False
        assert result["child_count"] == 0
        assert "blocked: needs decomposition" in result["reason"]

    def test_filters_out_parent_self_mention(self):
        """The parent issue itself (which might reference its own number) is not counted."""
        children = [_child(579, "the parent itself"), _child(580, "actual child")]
        result = gate.check_decomposition_gate(579, 0.5, runner=self._runner(children))
        assert result["passed"] is True
        assert result["child_count"] == 1
        children_out = result.get("children", [])
        assert all(c["number"] != 579 for c in children_out)

    def test_fails_open_when_gh_errors(self):
        """gh failure → fail-OPEN (pass). Never block on a transient error."""

        def run(cmd):
            return 1, "", "error: not authenticated"

        result = gate.check_decomposition_gate(579, 0.6, runner=run)
        assert result["passed"] is True
        assert result["child_count"] is None
        assert "failing open" in result["reason"]

    def test_fails_open_on_garbage_json(self):
        """Malformed gh output → fail-OPEN."""

        def run(cmd):
            return 0, "not-valid-json", ""

        result = gate.check_decomposition_gate(579, 0.6, runner=run)
        assert result["passed"] is True

    def test_exact_threshold(self):
        """effort exactly 0.4 triggers the check."""
        children = [_child(580)]
        result = gate.check_decomposition_gate(579, 0.4, runner=self._runner(children))
        assert result["passed"] is True
        assert result["child_count"] == 1


class TestFindChildIssues:
    """Test find_child_issues() directly."""

    def _runner(self, children_list, rc=0):
        def run(cmd):
            return rc, json.dumps(children_list), ""

        return run

    def test_returns_children_excluding_parent(self):
        children = [_child(579), _child(580)]
        result = gate.find_child_issues(579, runner=self._runner(children))
        assert result is not None
        assert len(result) == 1
        assert result[0]["number"] == 580

    def test_returns_empty_list_when_no_children(self):
        result = gate.find_child_issues(579, runner=self._runner([]))
        assert result == []

    def test_returns_none_on_gh_failure(self):
        def run(cmd):
            return 1, "", "gh: command not found"

        result = gate.find_child_issues(579, runner=run)
        assert result is None

    def test_search_query_contains_mentions(self):
        """Verify the search query is correctly formed."""
        captured: list = []

        def run(cmd):
            captured.extend(cmd)
            return 0, json.dumps([]), ""

        gate.find_child_issues(579, runner=run)
        cmd_str = " ".join(str(x) for x in captured)
        assert "mentions:#579" in cmd_str
        assert "is:open" in cmd_str


# ── CLI integration tests ──────────────────────────────────────────────────────


class TestCLI:
    def test_exit_0_when_pass(self, capsys, monkeypatch):
        """effort < 0.4 → exit 0 (pass)."""
        rc = gate.main(["prog", "check", "579", "--effort", "0.3"])
        out = json.loads(capsys.readouterr().out)
        assert rc == 0
        assert out["passed"] is True

    def test_exit_1_when_blocked(self, capsys, monkeypatch):
        """effort >= 0.4, no children → exit 1 (blocked)."""
        monkeypatch.setattr(
            gate, "_default_runner", lambda cmd: (0, json.dumps([]), "")
        )
        rc = gate.main(["prog", "check", "579", "--effort", "0.6"])
        out = json.loads(capsys.readouterr().out)
        assert rc == 1
        assert out["passed"] is False
        assert "blocked: needs decomposition" in out["reason"]

    def test_exit_0_when_children_found(self, capsys, monkeypatch):
        """effort >= 0.4, children found → exit 0 (pass)."""
        children = [_child(580, "slice 1")]
        monkeypatch.setattr(
            gate, "_default_runner", lambda cmd: (0, json.dumps(children), "")
        )
        rc = gate.main(["prog", "check", "579", "--effort", "0.5"])
        out = json.loads(capsys.readouterr().out)
        assert rc == 0
        assert out["passed"] is True

    def test_exit_2_no_effort(self):
        """Missing --effort → exit 2 (usage error)."""
        rc = gate.main(["prog", "check", "579"])
        assert rc == 2

    def test_exit_2_bad_effort(self):
        """Non-numeric --effort → exit 2."""
        rc = gate.main(["prog", "check", "579", "--effort", "banana"])
        assert rc == 2

    def test_exit_2_bad_issue(self):
        """Non-numeric issue_number → exit 2."""
        rc = gate.main(["prog", "check", "abc", "--effort", "0.5"])
        assert rc == 2

    def test_exit_2_unknown_command(self):
        """Unknown subcommand → exit 2."""
        rc = gate.main(["prog", "foo"])
        assert rc == 2

    def test_exit_2_no_args(self):
        """No args → exit 2."""
        rc = gate.main(["prog"])
        assert rc == 2
