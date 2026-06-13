"""Tests for agent/loop_guard.py — advisory loop / repeated-failure detection."""

from agent.loop_guard import current_run_signature, maybe_nudge


def _asst(tool, args="{}", call_id="c"):
    return {
        "role": "assistant",
        "tool_calls": [{"id": call_id, "function": {"name": tool, "arguments": args}}],
    }


def _result(content, call_id="c"):
    return {"role": "tool", "tool_call_id": call_id, "content": content}


def _run(tool, n, *, result="ok"):
    """n consecutive single-tool turns, each with a (non-failing) result."""
    msgs = [{"role": "user", "content": "do the thing"}]
    for i in range(n):
        cid = f"c{i}"
        msgs.append(_asst(tool, call_id=cid))
        msgs.append(_result(result, call_id=cid))
    return msgs


class TestRepeatTrigger:
    def test_below_threshold_is_quiet(self):
        assert maybe_nudge(_run("terminal", 5)) is None

    def test_at_threshold_nudges(self):
        n = maybe_nudge(_run("terminal", 6))
        assert n is not None and "terminal" in n and "loop-guard" in n

    def test_signature_counts_the_run(self):
        assert current_run_signature(_run("read_file", 4)) == ("read_file", 4)

    def test_no_tools_no_nudge(self):
        assert maybe_nudge([{"role": "user", "content": "hi"}]) is None


class TestFailureTrigger:
    def test_three_consecutive_failures_nudges_even_below_repeat(self):
        msgs = _run("terminal", 3, result="bash: foo: command not found")
        n = maybe_nudge(msgs)
        assert n is not None and "failed 3 times" in n

    def test_two_failures_not_enough(self):
        assert maybe_nudge(_run("terminal", 2, result="permission denied")) is None

    def test_exit_code_marker_counts_as_failure(self):
        msgs = _run("execute_code", 3, result="process finished, exit code: 1")
        assert maybe_nudge(msgs) is not None

    def test_mcp_unreachable_failures(self):
        msgs = _run("mcp_tqmemory_health", 3, result="server unreachable: ClosedResourceError")
        n = maybe_nudge(msgs)
        assert n is not None


class TestRunBoundaries:
    def test_tool_change_breaks_the_run(self):
        # 5x terminal then 1x read_file at the tail -> only the read_file run (len 1)
        msgs = _run("terminal", 5) + _run("read_file", 1)[1:]
        assert current_run_signature(msgs) == ("read_file", 1)
        assert maybe_nudge(msgs) is None

    def test_text_reply_breaks_the_run(self):
        msgs = _run("terminal", 8)
        msgs.append({"role": "assistant", "content": "Here is my summary."})
        assert maybe_nudge(msgs) is None  # the run was broken by a text turn

    def test_multi_tool_turn_breaks_the_run(self):
        msgs = _run("terminal", 8)
        msgs.append({
            "role": "assistant",
            "tool_calls": [
                {"id": "m1", "function": {"name": "read_file", "arguments": "{}"}},
                {"id": "m2", "function": {"name": "terminal", "arguments": "{}"}},
            ],
        })
        assert maybe_nudge(msgs) is None  # varied multi-tool work, not a spiral
