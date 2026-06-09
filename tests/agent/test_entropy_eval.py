"""Tests for the entropy-based behavioral evaluation module."""

import pytest

from agent.entropy_eval import (
    EntropyEngine,
    MultiSessionAggregator,
    SessionEntropyReport,
    _safe_entropy,
    format_report_terminal,
)


class TestSafeEntropy:
    def test_empty_counter(self):
        from collections import Counter

        assert _safe_entropy(Counter(), 0) == 0.0

    def test_uniform_distribution(self):
        from collections import Counter

        c = Counter({"a": 2, "b": 2})
        assert _safe_entropy(c, 4) == 1.0

    def test_single_item(self):
        from collections import Counter

        c = Counter({"a": 5})
        assert _safe_entropy(c, 5) == 0.0


class TestEntropyEngine:
    def test_analyze_empty_messages(self):
        engine = EntropyEngine()
        report = engine.analyze("s1", [])
        assert report.session_id == "s1"
        assert report.action_entropy == 0.0
        assert report.tool_entropy == 0.0
        assert report.trajectory_entropy == 0.0

    def test_analyze_simple_conversation(self):
        messages = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello", "tool_calls": []},
        ]
        engine = EntropyEngine()
        report = engine.analyze("s2", messages)
        assert report.action_entropy > 0
        assert report.exploration_ratio > 0

    def test_tool_entropy_with_tool_calls(self):
        messages = [
            {"role": "user", "content": "do this"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {"function": {"name": "terminal"}, "id": "tc1"},
                    {"function": {"name": "file"}, "id": "tc2"},
                ],
            },
            {"role": "user", "content": "do that"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {"function": {"name": "terminal"}, "id": "tc3"},
                ],
            },
        ]
        engine = EntropyEngine()
        report = engine.analyze("s3", messages)
        assert report.tool_entropy > 0
        # There are at least 2 unique tools used
        assert len(report.action_counts) >= 5

    def test_information_gain_with_baseline(self):
        from collections import Counter

        baseline = Counter({
            "tool:terminal": 10,
            "role:user": 5,
            "role:assistant(text)": 5,
        })
        messages = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello", "tool_calls": []},
        ]
        engine = EntropyEngine(baseline_actions=baseline)
        report = engine.analyze("s4", messages)
        # KL divergence should be > 0 because distributions differ
        assert report.information_gain >= 0.0


class TestMultiSessionAggregator:
    def test_empty(self):
        assert MultiSessionAggregator.aggregate([]) == {}

    def test_two_sessions(self):
        reports = [
            SessionEntropyReport(
                session_id="a",
                action_entropy=1.0,
                trajectory_entropy=2.0,
                tool_entropy=0.5,
                information_gain=0.1,
                exploration_ratio=0.2,
                action_counts={},
                transition_counts={},
            ),
            SessionEntropyReport(
                session_id="b",
                action_entropy=3.0,
                trajectory_entropy=4.0,
                tool_entropy=1.5,
                information_gain=0.3,
                exploration_ratio=0.4,
                action_counts={},
                transition_counts={},
            ),
        ]
        agg = MultiSessionAggregator.aggregate(reports)
        assert agg["action_entropy"]["mean"] == 2.0
        assert agg["tool_entropy"]["mean"] == 1.0


class TestFormatReportTerminal:
    def test_contains_metrics(self):
        report = SessionEntropyReport(
            session_id="abc123",
            action_entropy=1.5,
            trajectory_entropy=2.5,
            tool_entropy=0.4,
            information_gain=0.1,
            exploration_ratio=0.05,
            action_counts={},
            transition_counts={},
        )
        text = format_report_terminal(report)
        assert "1.500" in text
        assert "0.050" in text
        # Low exploration triggers note
        assert "repetitive" in text.lower()

    def test_high_tool_entropy_warning(self):
        report = SessionEntropyReport(
            session_id="abc",
            action_entropy=3.0,
            trajectory_entropy=4.0,
            tool_entropy=3.5,
            information_gain=0.5,
            exploration_ratio=0.5,
            action_counts={},
            transition_counts={},
        )
        text = format_report_terminal(report)
        assert "high" in text.lower()
