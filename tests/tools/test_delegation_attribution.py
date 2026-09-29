# -*- coding: utf-8 -*-
"""Tests for delegation attribution markers (issue #67, slice 1).

Covers the canonical marker format (build / parse / embed) and the real
call-site wiring in :func:`tools.delegate_tool._build_child_system_prompt`:
every delegated child's system prompt carries its attribution stamp and the
artifact-stamping instruction, while a prompt built without a stamp is
byte-identical in shape to the pre-#67 behavior (no ATTRIBUTION section).
"""

from __future__ import annotations

import pytest

from tools.delegation_attribution import (
    ATTRIBUTION_MARKER,
    AttributionStamp,
    attribution_prompt_block,
    build_attribution_stamp,
    parse_attribution_stamp,
    stamp_for_artifact_header,
)


class TestBuildStamp:
    def test_first_level_child_marks_parent_root(self):
        stamp = build_attribution_stamp(subagent_id="sa-0-abc12345")
        assert stamp.startswith(ATTRIBUTION_MARKER)
        assert "subagent_id=sa-0-abc12345" in stamp
        assert "parent=root" in stamp
        assert "task_index=-" in stamp

    def test_nested_child_marks_parent_subagent(self):
        stamp = build_attribution_stamp(
            subagent_id="sa-1-def67890",
            parent_subagent_id="sa-0-abc12345",
            task_index=3,
        )
        assert "parent=sa-0-abc12345" in stamp
        assert "task_index=3" in stamp

    def test_spawned_at_can_be_pinned(self):
        stamp = build_attribution_stamp(
            subagent_id="sa-0-x", spawned_at="2026-08-20T10:00:00+00:00"
        )
        assert "spawned_at=2026-08-20T10:00:00+00:00" in stamp

    def test_rejects_empty_subagent_id(self):
        with pytest.raises(ValueError):
            build_attribution_stamp(subagent_id="")


class TestParseStamp:
    def test_roundtrip(self):
        original = build_attribution_stamp(
            subagent_id="sa-7-feedbeef",
            parent_subagent_id="sa-2-cafebabe",
            task_index=7,
        )
        parsed = parse_attribution_stamp(original)
        assert parsed is not None
        assert parsed.subagent_id == "sa-7-feedbeef"
        assert parsed.parent_subagent_id == "sa-2-cafebabe"
        assert parsed.task_index == 7
        assert parsed.spawned_at  # non-empty ISO timestamp

    def test_root_parent_parses_to_none(self):
        stamp = build_attribution_stamp(subagent_id="sa-0-rootkid")
        parsed = parse_attribution_stamp(stamp)
        assert parsed is not None
        assert parsed.parent_subagent_id is None

    def test_non_marker_text_returns_none(self):
        for bad in (None, "", "random text", "HERMES-SUBAGENT", "# a comment"):
            assert parse_attribution_stamp(bad) is None

    def test_malformed_marker_returns_none(self):
        # Marker present but no subagent_id key.
        assert parse_attribution_stamp(f"{ATTRIBUTION_MARKER} parent=root") is None

    def test_extra_keys_ignored(self):
        parsed = parse_attribution_stamp(
            f"{ATTRIBUTION_MARKER} subagent_id=sa-0-x future_key=whatever"
        )
        assert parsed is not None
        assert parsed.subagent_id == "sa-0-x"


class TestEmbedding:
    def test_prompt_block_carries_marker_and_instruction(self):
        stamp = build_attribution_stamp(subagent_id="sa-0-xyz")
        block = attribution_prompt_block(stamp)
        assert ATTRIBUTION_MARKER in block
        assert "sa-0-xyz" in block
        assert "Stamp every artifact" in block

    def test_artifact_header_line(self):
        stamp = build_attribution_stamp(subagent_id="sa-0-xyz")
        header = stamp_for_artifact_header(stamp)
        assert header.startswith("# ")
        assert ATTRIBUTION_MARKER in header


class TestDelegateToolWiring:
    """Integration with the real call site (the child system prompt)."""

    def test_child_prompt_carries_attribution_when_stamped(self):
        from tools.delegate_tool import _build_child_system_prompt

        stamp = build_attribution_stamp(
            subagent_id="sa-5-integration", parent_subagent_id=None, task_index=5
        )
        prompt = _build_child_system_prompt(
            "Do the thing",
            context="Some context",
            attribution=stamp,
        )
        assert "ATTRIBUTION:" in prompt
        assert stamp in prompt
        assert "Some context" in prompt

    def test_child_prompt_unchanged_without_attribution(self):
        from tools.delegate_tool import _build_child_system_prompt

        prompt = _build_child_system_prompt("Do the thing", context="Some context")
        assert "ATTRIBUTION:" not in prompt
        assert ATTRIBUTION_MARKER not in prompt
        assert "Some context" in prompt

    def test_attribution_stamp_roundtrip_through_prompt(self):
        """The marker embedded in a prompt is still parseable (verifiability)."""
        from tools.delegate_tool import _build_child_system_prompt

        stamp = build_attribution_stamp(subagent_id="sa-9-parseback", task_index=2)
        prompt = _build_child_system_prompt("Go", attribution=stamp)
        # Pull the marker line out of the prompt and parse it back.
        line = next(
            line for line in prompt.splitlines() if line.startswith(ATTRIBUTION_MARKER)
        )
        parsed = parse_attribution_stamp(line)
        assert parsed is not None
        assert parsed.subagent_id == "sa-9-parseback"
        assert parsed.task_index == 2
