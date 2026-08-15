# -*- coding: utf-8 -*-
"""Unit tests for Multi-Scale Context Folding (AgentFold, issue #2361)."""

import pytest
from agent.context_folding import (
    ContextFoldLevel,
    FoldedContextSummary,
    MultiScaleContextFolder,
)


class TestMultiScaleContextFolder:
    """Test suite for layered multi-scale context compression."""

    def test_short_history_passes_through(self):
        folder = MultiScaleContextFolder(granular_window_turns=4, deep_window_turns=10)
        messages = [
            {"role": "system", "content": "You are Hermes."},
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi there!"},
        ]
        folded, summary = folder.fold_context(messages)
        assert len(folded) == 3
        assert summary.deep_summary == ""
        assert summary.granular_summary == ""
        assert summary.working_messages_count == 2

    def test_multiscale_partitioning_and_compression(self):
        folder = MultiScaleContextFolder(granular_window_turns=2, deep_window_turns=5)

        # Generate 8 full turn blocks (user -> assistant with tool -> tool result)
        messages = [{"role": "system", "content": "You are Hermes."}]
        for i in range(8):
            messages.extend([
                {"role": "user", "content": f"Task step {i}: find file {i}"},
                {
                    "role": "assistant",
                    "content": f"Executing search {i}",
                    "tool_calls": [
                        {
                            "id": f"call_{i}",
                            "function": {
                                "name": "file_search",
                                "arguments": f'{{"pattern": "file_{i}.py"}}',
                            },
                        }
                    ],
                },
                {"role": "tool", "content": f"Found file_{i}.py at /path/{i}"},
            ])

        folded, summary = folder.fold_context(messages)

        # Total original messages = 1 + 8*3 = 25
        assert summary.total_original_messages == 25
        # Deep summary contains older turns (turns 0, 1, 2)
        assert "Task step 0" in summary.deep_summary
        # Granular summary contains medium turns (turns 3, 4, 5)
        assert "file_search" in summary.granular_summary
        # Working messages contains recent turns (turns 6, 7 -> 6 non-system messages)
        assert summary.working_messages_count == 6
        # System memory block injected
        system_blocks = [m for m in folded if m.get("role") == "system"]
        assert any(
            "MULTI-SCALE SESSION MEMORY" in m.get("content", "") for m in system_blocks
        )
        # Token savings estimated
        assert summary.estimated_tokens_saved > 0

    def test_empty_messages(self):
        folder = MultiScaleContextFolder()
        folded, summary = folder.fold_context([], system_prompt="System prompt")
        assert len(folded) == 1
        assert folded[0]["content"] == "System prompt"
        assert summary.total_original_messages == 0
