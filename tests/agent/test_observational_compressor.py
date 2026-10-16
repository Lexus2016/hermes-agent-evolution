"""Unit tests for observational context compression (issues #3066, #3068, #3069)."""

import pytest

from agent.observational_compressor import (
    ObservationalCompressionConfig,
    ObservationalContextEngine,
    distill_terminal_output,
)
from plugins.context_engine import discover_context_engines


def test_distill_terminal_output_strips_ansi():
    raw = "\x1b[31mError:\x1b[0m File not found\n\x1b[32mSuccess\x1b[0m"
    distilled = distill_terminal_output(raw)
    assert "\x1b" not in distilled
    assert "Error: File not found" in distilled
    assert "Success" in distilled


def test_distill_terminal_output_compacts_progress():
    lines = [f"[===>      ] {i}% downloading..." for i in range(10, 90, 5)]
    raw = "Starting download\n" + "\n".join(lines) + "\nDownload complete (exit code: 0)"
    distilled = distill_terminal_output(raw)
    assert "Starting download" in distilled
    assert "Download complete (exit code: 0)" in distilled
    # Most intermediate progress bars should be stripped
    assert distilled.count("downloading...") <= 2


def test_distill_terminal_output_dedups_polling():
    lines = ["Waiting for job 48219 to finish..."] * 20
    raw = "Job dispatched\n" + "\n".join(lines) + "\nJob 48219 succeeded with status OK"
    distilled = distill_terminal_output(raw)
    assert "Job dispatched" in distilled
    assert "Job 48219 succeeded with status OK" in distilled
    assert "similar status lines suppressed" in distilled


def test_distill_terminal_output_preserves_head_and_tail():
    lines = [f"Setup step {i}: initial config" for i in range(10)]
    lines += [f"Intermediate compiler step {i} running" for i in range(100)]
    lines += ["Final error at line 42: Assertion failed", "Exit code: 1"]
    raw = "\n".join(lines)

    distilled = distill_terminal_output(raw, max_head_lines=5, max_tail_lines=5)
    assert "Setup step 0" in distilled
    assert "Final error at line 42: Assertion failed" in distilled
    assert "Exit code: 1" in distilled
    assert "intermediate output lines distilled" in distilled


def test_observational_context_engine_compression():
    engine = ObservationalContextEngine(
        ObservationalCompressionConfig(token_threshold=100, tail_turns_to_preserve=2)
    )
    engine.on_session_start("sess-1")

    # Long repetitive tool output in turn 1
    long_tool_output = "\n".join([f"Processing item {i}" for i in range(500)])
    messages = [
        {"role": "system", "content": "You are Hermes."},
        {"role": "user", "content": "Run the task"},
        {"role": "tool", "content": long_tool_output},
        {"role": "assistant", "content": "Step 1 done"},
        {"role": "user", "content": "Next step"},
        {"role": "assistant", "content": "Currently executing..."},
    ]

    assert engine.should_compress(messages) is True

    compressed_msgs, stats = engine.compress(messages)
    assert stats["compressed"] is True
    assert stats["tokens_saved"] > 0
    assert stats["compression_ratio"] < 0.60

    # System prompt preserved byte-for-byte
    assert compressed_msgs[0] == messages[0]

    # Recent tail turns preserved intact
    assert compressed_msgs[-1] == messages[-1]
    assert compressed_msgs[-2] == messages[-2]

    # Historical tool message was distilled
    assert len(compressed_msgs[2]["content"]) < len(long_tool_output)

    engine.on_session_end()


def test_context_engine_discovery():
    engines = discover_context_engines()
    names = [e[0] for e in engines]
    assert "observational" in names
