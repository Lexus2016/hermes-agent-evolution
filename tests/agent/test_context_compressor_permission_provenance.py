"""A summary may not invent the authority a successor session will act on.

The summarizer is told that permissions and prohibitions are only recordable when the source turns state
them, and that a recorded requirement names its origin. Without that rule a summary reads as the record, so
an inferred "X is allowed" becomes an approval the prior work never granted.
"""

from unittest.mock import patch

from agent.context_compressor import ContextCompressor

_RULE = "A permission or a prohibition belongs in the summary only if the turns below state it"
_ORIGIN = "name where it came from"


def _compressor() -> ContextCompressor:
    with patch("agent.context_compressor.get_model_context_length", return_value=100000):
        return ContextCompressor(model="test", quiet_mode=True)


def test_fresh_summary_prompt_names_the_permission_rule():
    prompt = _compressor()._build_summary_prompt("A turn", 500, None, "", True)
    assert _RULE in prompt
    assert _ORIGIN in prompt
    assert "PREVIOUS SUMMARY:" not in prompt


def test_iterative_summary_prompt_keeps_the_permission_rule():
    compressor = _compressor()
    compressor._previous_summary = "## Goal\nEarlier work"
    prompt = compressor._build_summary_prompt("A new turn", 500, None, "", True)
    assert "PREVIOUS SUMMARY:" in prompt
    assert _RULE in prompt
    assert _ORIGIN in prompt
