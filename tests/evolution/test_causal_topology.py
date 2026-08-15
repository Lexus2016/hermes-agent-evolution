# -*- coding: utf-8 -*-
"""Unit tests for causal topology discovery over Hydra dispatch (issue #2438)."""

import json
from pathlib import Path
import pytest

from evolution.lib.causal_topology import (
    DispatchEdge,
    DispatchEvent,
    DispatchTopologyAnalyzer,
    TopologyAnalysisReport,
)


class TestCausalTopology:
    """Test suite for DispatchTopologyAnalyzer and causal discovery."""

    def test_serialization(self):
        event = DispatchEvent(
            tick=10,
            trigger_signal="gate_passed",
            stage="synthesis",
            artifacts_read=["analysis.json"],
            artifacts_written=["proposal.patch"],
            outcome="real",
        )
        d = event.to_dict()
        assert d["tick"] == 10
        assert d["trigger_signal"] == "gate_passed"

        restored = DispatchEvent.from_dict(d)
        assert restored.tick == event.tick
        assert restored.artifacts_written == ["proposal.patch"]

        edge = DispatchEdge(
            source_signal="gate_passed",
            target_stage="synthesis",
            total_fires=10,
            real_outcomes=8,
            noop_outcomes=2,
        )
        score = edge.compute_score()
        assert score == 0.8
        assert edge.to_dict()["causal_effect_score"] == 0.8

    def test_prune_stale_rewake_edges(self):
        analyzer = DispatchTopologyAnalyzer()

        # Simulate 10 ticks:
        # Edge 1: ("commit_pushed" -> "triage") produces 9 real outcomes out of 10
        # Edge 2: ("stale_timer" -> "recheck") produces 10 noop outcomes out of 10 (stale re-wake loop like 2026-08-14 incident)
        events = []
        for tick in range(1, 11):
            events.append(
                DispatchEvent(
                    tick=tick,
                    trigger_signal="commit_pushed",
                    stage="triage",
                    outcome="real" if tick != 5 else "noop",
                )
            )
            events.append(
                DispatchEvent(
                    tick=tick,
                    trigger_signal="stale_timer",
                    stage="recheck",
                    outcome="noop",
                )
            )

        analyzer.ingest_events(events)
        assert analyzer.compute_noop_rate() == 0.55  # 11 noops out of 20 events

        report = analyzer.analyze_causal_topology(
            min_effect_threshold=0.15,
            min_sample_size=4,
        )

        assert report.total_ticks == 10
        assert report.total_events == 20
        assert len(report.prune_candidates) == 1
        assert report.prune_candidates[0].source_signal == "stale_timer"
        assert report.prune_candidates[0].target_stage == "recheck"
        assert report.prune_candidates[0].causal_effect_score == 0.0

    def test_batch_candidates_detection(self):
        analyzer = DispatchTopologyAnalyzer()

        # Simulate stages "lint_check" and "type_check" always firing together on "pr_opened"
        for tick in range(1, 8):
            analyzer.record_event(
                DispatchEvent(
                    tick=tick,
                    trigger_signal="pr_opened",
                    stage="lint_check",
                    outcome="real",
                )
            )
            analyzer.record_event(
                DispatchEvent(
                    tick=tick,
                    trigger_signal="pr_opened",
                    stage="type_check",
                    outcome="real",
                )
            )

        report = analyzer.analyze_causal_topology(min_sample_size=5)
        assert len(report.batch_candidates) >= 1
        assert sorted(report.batch_candidates[0]) == ["lint_check", "type_check"]

    def test_jsonl_ingestion(self, tmp_path: Path):
        jsonl_file = tmp_path / "dispatch_log.jsonl"
        records = [
            {"tick": 1, "trigger_signal": "sig1", "stage": "stage1", "outcome": "real"},
            {"tick": 1, "trigger_signal": "sig1", "stage": "stage2", "outcome": "noop"},
            {"tick": 2, "trigger_signal": "sig2", "stage": "stage1", "outcome": "real"},
        ]
        with open(jsonl_file, "w", encoding="utf-8") as f:
            for r in records:
                f.write(json.dumps(r) + "\n")

        analyzer = DispatchTopologyAnalyzer()
        count = analyzer.ingest_from_jsonl(jsonl_file)
        assert count == 3
        assert len(analyzer.events) == 3
        assert analyzer.compute_noop_rate() == round(1 / 3, 4)
