"""Cron deliveries must contain the RESULT only — never model reasoning."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from cron.scheduler import strip_reasoning_for_delivery as strip  # noqa: E402


def test_strips_think_block():
    assert strip("<think>let me reason</think>\n# Report\nresult") == "# Report\nresult"


def test_strips_multiline_and_case_insensitive():
    txt = "<Thinking>\nplan\nstep\n</Thinking>\n\nThe answer is 42."
    assert strip(txt) == "The answer is 42."


def test_strips_reasoning_and_scratchpad_tags():
    assert strip("<reasoning>x</reasoning>RESULT") == "RESULT"
    assert strip("<REASONING_SCRATCHPAD>y</REASONING_SCRATCHPAD>\nok") == "ok"


def test_strips_multiple_blocks():
    assert strip("<think>a</think>line1\n<think>b</think>line2") == "line1\nline2"


def test_clean_text_unchanged():
    rep = "# Research Report - 2026-06-14\n\n## New Features\n- X"
    assert strip(rep) == rep


def test_empty_and_none_safe():
    assert strip("") == ""
    assert strip(None) is None
