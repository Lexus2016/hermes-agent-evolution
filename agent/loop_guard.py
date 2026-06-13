"""Loop / repeated-failure guard for the agent tool-calling loop.

Addresses a whole cluster of observed failure modes where the agent stops making
progress and keeps hammering the same tool:

  * same single tool called many turns in a row with no progress (#173)
  * terminal commands repeatedly failing on missing prereqs / errors (#174)
  * hard limits / access denials retried instead of routed around (#175)
  * an unreachable MCP server looped on health checks (#176)
  * spirals that eventually hit the max-iteration abort (#143)

Mechanism (deliberately conservative — advisory, never blocking):
inspect the most recent CONSECUTIVE assistant tool-call turns. If the SAME tool
is used `repeat_threshold` times in a row, or its last `fail_threshold` results
look like failures, return a one-time nudge string. The caller injects it as a
user-role message (the codebase's mid-loop guidance pattern) telling the model
to stop, re-check the goal, and change strategy. A real loop is broken; a rare
false positive costs one advisory message.

Pure functions over the `messages` list → fully unit-testable, no agent state
required (the caller tracks "already nudged this run" to avoid spamming).
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

# Failure shapes cited in the cluster issues. Matched case-insensitively against
# tool result content. Kept specific to avoid flagging benign output.
_FAILURE_MARKERS = (
    "command not found",
    "no such file",
    "permission denied",
    "access denied",
    "refusing to write",
    "forbidden",
    "timed out",
    "timeout",
    "traceback (most recent call",
    "closedresourceerror",
    "unreachable",
    "externally-managed-environment",
    "error:",
    "failed",
    "exit status",
    "is not recognized",
    "could not be found",
)

_EXIT_CODE_RE = re.compile(r"exit code[:\s]+([1-9]\d*)", re.IGNORECASE)


def _looks_like_failure(content: Any) -> bool:
    if not isinstance(content, str) or not content:
        return False
    low = content.lower()
    if any(m in low for m in _FAILURE_MARKERS):
        return True
    return bool(_EXIT_CODE_RE.search(content))


def _recent_tool_runs(messages: List[Dict[str, Any]]) -> List[Tuple[str, bool]]:
    """Most-recent-first list of (single_tool_name, result_failed) for the
    trailing run of assistant turns that each called EXACTLY ONE tool.

    Stops at the first assistant turn that is not a single-tool call (a text
    reply, or a multi-tool turn) — that breaks the "stuck on one tool" run.
    Multi-tool turns are normal varied work, not a single-tool spiral.
    """
    runs: List[Tuple[str, bool]] = []
    i = len(messages) - 1
    # Collect tool results by id as we walk back so we can mark failures.
    while i >= 0:
        msg = messages[i]
        if msg.get("role") == "assistant" and msg.get("tool_calls"):
            tcs = [tc for tc in msg["tool_calls"] if isinstance(tc, dict)]
            names = [
                tc.get("function", {}).get("name")
                for tc in tcs
                if tc.get("function")
            ]
            names = [n for n in names if n]
            if len(set(names)) != 1:
                break  # text turn or multi-tool turn — run ends
            tool = names[0]
            if runs and tool != runs[0][0]:
                break  # tool changed — the same-tool run ends here
            # Results for this turn are the "tool" messages that follow it.
            failed = False
            for j in range(i + 1, len(messages)):
                tm = messages[j]
                if tm.get("role") != "tool":
                    break
                if _looks_like_failure(tm.get("content")):
                    failed = True
            runs.append((tool, failed))
            i -= 1
        elif msg.get("role") == "tool":
            i -= 1  # skip result messages; handled with their assistant turn
        else:
            break  # user/system/text-assistant turn breaks the run
    return runs


def maybe_nudge(
    messages: List[Dict[str, Any]],
    *,
    repeat_threshold: int = 6,
    fail_threshold: int = 3,
) -> Optional[str]:
    """Return a nudge string if the trailing single-tool run is stuck, else None.

    Two triggers (failure takes precedence — it's the higher-signal one):
      * the same tool's last `fail_threshold` results all look like failures
      * the same tool was called `repeat_threshold`+ times in a row
    """
    runs = _recent_tool_runs(messages)
    if not runs:
        return None
    tool = runs[0][0]
    # All entries in `runs` share the same tool (run breaks on tool change),
    # but guard anyway:
    same = [r for r in runs if r[0] == tool]
    count = len(same)
    consec_fail = 0
    for _t, failed in same:
        if failed:
            consec_fail += 1
        else:
            break

    if consec_fail >= fail_threshold:
        return (
            f"[loop-guard] The `{tool}` tool has failed {consec_fail} times in a "
            f"row with the same approach. STOP repeating it. Diagnose the actual "
            f"blocker first (check prerequisites / environment / the exact error "
            f"class), then either switch to a different tool or strategy, or — if "
            f"the blocker can't be resolved — report it concisely instead of "
            f"retrying. Do not call `{tool}` again the same way."
        )
    if count >= repeat_threshold:
        return (
            f"[loop-guard] You have called `{tool}` {count} times in a row without "
            f"resolving the task. Pause and re-read the goal: what concrete "
            f"progress have these calls made? Check your plan/success criterion, "
            f"then either change strategy, move to the next step, or report the "
            f"blocker. Avoid another near-identical `{tool}` call."
        )
    return None


def current_run_signature(messages: List[Dict[str, Any]]) -> Optional[Tuple[str, int]]:
    """(tool, count) of the trailing single-tool run, or None. Callers use this
    to nudge once per escalating run instead of every iteration."""
    runs = _recent_tool_runs(messages)
    if not runs:
        return None
    return (runs[0][0], len(runs))
