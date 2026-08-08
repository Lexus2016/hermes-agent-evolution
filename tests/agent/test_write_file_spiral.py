"""Tests for write_file spiral-prone tool registration (#1840).

write_file was missing from _SPIRAL_PRONE_TOOLS, so parse-error failures
(structural syntax validation failures) never accumulated toward the
spiral_failure_cap. This test verifies:
1. write_file is in the spiral-prone set (so the cap fires)
2. write_file has a fallback directive defined
"""

from agent.tool_guardrails import (
    ToolCallGuardrailConfig,
    _TOOL_FALLBACK_DIRECTIVE,
    ToolCallGuardrailController,
)


def test_write_file_is_spiral_prone():
    """write_file must be in _SPIRAL_PRONE_TOOLS so parse-error spirals
    accumulate toward the cap (#1840)."""
    cfg = ToolCallGuardrailConfig()
    assert "write_file" in cfg.spiral_prone_tools


def test_write_file_has_fallback_directive():
    """write_file must have a fallback directive for when the cap fires."""
    assert "write_file" in _TOOL_FALLBACK_DIRECTIVE
    assert len(_TOOL_FALLBACK_DIRECTIVE["write_file"]) > 10


def test_write_file_spiral_cap_fires():
    """Consecutive write_file failures must trigger the spiral cap.
    After reaching the cap, the tool is session-hard-stopped — an even
    stronger enforcement than the per-turn cap."""
    controller = ToolCallGuardrailController()
    for _ in range(5):
        controller.before_call("write_file", {"path": "/tmp/test.json"})
        controller.after_call(
            "write_file",
            {"path": "/tmp/test.json"},
            '{"error": "Refusing to write: syntax validation failed"}',
        )
    d = controller.before_call("write_file", {"path": "/tmp/test.json"})
    assert d.action == "block"
    assert d.code in ("spiral_prone_tool_failure_cap", "session_hard_stop")
