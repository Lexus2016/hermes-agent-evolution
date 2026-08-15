# -*- coding: utf-8 -*-
"""Unit tests for Sleep-Time Compute memory consolidator (#2358)."""

import pytest

from evolution.lib.sleep_time_consolidator import (
    ConsolidationAction,
    ConsolidationReport,
    SleepTimeMemoryConsolidator,
)


class TestSleepTimeMemoryConsolidator:
    """Test suite for offline memory consolidation."""

    def test_report_serialization(self):
        report = ConsolidationReport(
            promoted_count=2,
            deprecated_count=1,
            merged_count=3,
            linked_count=1,
            actions=[
                ConsolidationAction(
                    action_type="promote",
                    target_id="note_1",
                    reason="high access frequency",
                )
            ],
        )
        d = report.to_dict()
        assert d["promoted_count"] == 2
        assert len(d["actions"]) == 1
        assert d["actions"][0]["target_id"] == "note_1"

    def test_deduplication_and_merging(self):
        notes = [
            {
                "id": "n1",
                "content": "Always run pytest before pushing commits",
                "tier": "durable",
            },
            {
                "id": "n2",
                "content": "always run pytest before   pushing commits",
                "tier": "episodic",
            },
            {"id": "n3", "content": "Different content", "tier": "episodic"},
        ]
        consolidated, report = SleepTimeMemoryConsolidator.consolidate_notes(notes)
        assert len(consolidated) == 2
        assert report.merged_count == 1
        assert report.actions[0].action_type == "merge"
        assert report.actions[0].target_id == "n2"

    def test_promotion_on_access_frequency(self):
        notes = [
            {
                "id": "n1",
                "content": "Frequently accessed pattern",
                "tier": "episodic",
                "access_count": 5,
            },
            {
                "id": "n2",
                "content": "Rarely accessed pattern",
                "tier": "episodic",
                "access_count": 1,
            },
        ]
        consolidated, report = SleepTimeMemoryConsolidator.consolidate_notes(
            notes, access_frequency_threshold=3
        )
        assert report.promoted_count == 1
        assert report.actions[0].action_type == "promote"
        assert report.actions[0].target_id == "n1"

        promoted_note = [n for n in consolidated if n["id"] == "n1"][0]
        assert promoted_note["tier"] == "durable"

    def test_link_detection(self):
        notes = [
            {
                "id": "n_new",
                "content": "This strategy supersedes note_old with better caching.",
                "tier": "durable",
            },
        ]
        consolidated, report = SleepTimeMemoryConsolidator.consolidate_notes(notes)
        assert report.linked_count == 1
        assert report.actions[0].action_type == "link"
        assert report.actions[0].metadata["relation_type"] == "supersedes"
