"""Tests for delegation provenance preservation across context compression (#1388).

Verifies that:
1. The compaction summary template includes a "Delegations Performed" section
2. The delegate_task tool result summarizer extracts sub-agent provenance
"""

import json

from agent.context_compressor import _summarize_tool_result


class TestDelegateProvenanceSummarizer:
    """_summarize_tool_result should extract sub-agent summaries from delegate_task results."""

    def test_extracts_provenance_from_delegation_result(self):
        """When delegate_task result contains summaries, they appear in the compressed view."""
        result_content = json.dumps({
            "results": [
                {"task": 0, "summary": "Found the bug in auth.py:42"},
                {"task": 1, "summary": "Created PR #123 with the fix"},
            ]
        })
        args = {"goal": "Fix the auth bug in the API"}
        summary = _summarize_tool_result("delegate_task", json.dumps(args), result_content)

        assert "Found the bug in auth.py:42" in summary
        assert "Created PR #123" in summary

    def test_handles_error_results(self):
        """Error results from sub-agents are preserved in provenance."""
        result_content = json.dumps({
            "results": [
                {"task": 0, "error": "Sub-agent failed: no shell access"},
            ]
        })
        args = {"goal": "Some task"}
        summary = _summarize_tool_result("delegate_task", json.dumps(args), result_content)

        assert "no shell access" in summary

    def test_handles_empty_results(self):
        """Empty or malformed delegation results fall back gracefully."""
        args = {"goal": "Do something"}
        summary = _summarize_tool_result("delegate_task", json.dumps(args), "{}")

        # Should still have the base summary format without provenance
        assert "[delegate_task]" in summary
        assert "→" not in summary

    def test_handles_non_json_content(self):
        """Non-JSON content doesn't crash the summarizer."""
        args = {"goal": "Do something"}
        summary = _summarize_tool_result("delegate_task", json.dumps(args), "not json at all")

        assert "[delegate_task]" in summary
        assert "→" not in summary

    def test_truncates_long_summaries(self):
        """Each provenance hint is capped to avoid blowing up the summary."""
        long_summary = "x" * 500
        result_content = json.dumps({
            "results": [
                {"task": 0, "summary": long_summary},
            ]
        })
        args = {"goal": "Do something"}
        summary = _summarize_tool_result("delegate_task", json.dumps(args), result_content)

        # The provenance hint should be capped at ~80 chars per entry
        assert "→" in summary
        assert long_summary not in summary


class TestSummaryTemplateHasDelegationSection:
    """The compaction summary template must include a Delegations Performed section."""

    def test_template_includes_delegation_section(self):
        """The summary prompt should instruct the model to preserve delegation history."""
        # Read the compress method's template by checking the module source
        import inspect

        from agent import context_compressor

        source = inspect.getsource(context_compressor)
        assert "Delegations Performed" in source
        assert "sub-agent delegation" in source.lower() or "delegation" in source.lower()
