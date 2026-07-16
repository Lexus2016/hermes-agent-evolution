"""Tests for the read_file identical-argument short-circuit (#1092).

``read_file`` is the second-largest retry-spiral cluster in the agent's own
traces (up to 8 consecutive identical calls). Re-reading the SAME
path/offset/limit returns bytes the model already has, so — like a repeated
search query — it should trip the same-argument short-circuit at
``_SHORT_CIRCUIT_REPEAT_THRESHOLD`` (4) rather than the generic idempotent
repeat_threshold (8), with a file-appropriate nudge (offset/limit), not the
search "rephrase the query" wording.
"""

import json

from agent.loop_guard import maybe_nudge


def _assistant_read_file(args: dict) -> dict:
    return {
        "role": "assistant",
        "tool_calls": [
            {
                "id": "call_read_file",
                "type": "function",
                "function": {
                    "name": "read_file",
                    "arguments": json.dumps(args),
                },
            }
        ],
    }


def _read_result(content: str = "line 1\nline 2\nline 3\n") -> dict:
    # A SUCCESSFUL read — the point is that repeating an identical read that
    # keeps succeeding is still a non-progressing loop.
    return {"role": "tool", "tool_call_id": "call_read_file", "content": content}


def _identical_read_run(n: int, args: dict | None = None) -> list[dict]:
    args = args or {"path": "src/app.py", "offset": 0, "limit": 100}
    msgs: list[dict] = []
    for _ in range(n):
        msgs.append(_assistant_read_file(args))
        msgs.append(_read_result())
    return msgs


class TestReadFileIdenticalShortCircuit:
    def test_trips_at_4_identical_reads(self):
        """4 identical read_file calls trip the same-argument short-circuit."""
        nudge = maybe_nudge(_identical_read_run(4))
        assert nudge is not None
        assert "read_file" in nudge

    def test_no_trip_at_3_identical_reads(self):
        """Below the short-circuit threshold (4) and the idempotent repeat (8),
        no nudge fires — a couple of re-reads is not yet a spiral."""
        assert maybe_nudge(_identical_read_run(3)) is None

    def test_varying_offsets_do_not_short_circuit_at_4(self):
        """Reading DIFFERENT ranges is legitimate progress — the identical-arg
        short-circuit must NOT fire when offset/limit change each call."""
        msgs: list[dict] = []
        for i in range(4):
            msgs.append(
                _assistant_read_file({"path": "src/app.py", "offset": i * 100, "limit": 100})
            )
            msgs.append(_read_result())
        assert maybe_nudge(msgs) is None

    def test_nudge_speaks_in_file_terms_not_search(self):
        """The read_file nudge must mention offset/limit and NOT tell the model
        to 'rephrase the query' (which does not apply to a file read)."""
        nudge = maybe_nudge(_identical_read_run(4))
        assert nudge is not None
        assert "offset/limit" in nudge
        assert "rephrase the query" not in nudge.lower()
