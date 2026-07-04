"""Loop / repeated-failure guard for the agent tool-calling loop.

Addresses a whole cluster of observed failure modes where the agent stops making
progress and keeps hammering the same tool:

  * same single tool called many turns in a row with no progress (#173)
  * terminal commands repeatedly failing on missing prereqs / errors (#174)
  * hard limits / access denials retried instead of routed around (#175)
  * an unreachable MCP server looped on health checks (#176)
  * spirals that eventually hit the max-iteration abort (#143)
  * mono-tool spirals where the agent fixates on ONE tool category (#432)

Mechanism (deliberately conservative — advisory, never blocking):
inspect the most recent CONSECUTIVE assistant tool-call turns. If the SAME tool
is used `repeat_threshold` times in a row, or its last `fail_threshold` results
look like failures, return a one-time nudge string. The caller injects it as a
user-role message (the codebase's mid-loop guidance pattern) telling the model
to stop, re-check the goal, and change strategy. A real loop is broken; a rare
false positive costs one advisory message.

Tools are split into two categories for thresholding:
- Mutating tools (terminal, write_file, patch, execute_code, etc.) get LOWER
  thresholds because a fixation on these is more costly and the model should
  be stopped sooner (#432).
- Idempotent tools (read_file, search_files, web_search, etc.) use the default
  higher thresholds since re-reading data is less harmful and sometimes needed.

At higher call counts, the nudge escalates from advisory to a DIRECTIVE that
requires the model to explain progress before continuing (#432).

Pure functions over the ``messages`` list -> fully unit-testable, no agent state
required (the caller tracks "already nudged this run" to avoid spamming).
"""

from __future__ import annotations

import json
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
    "no results",
    "no results found",
)

_EXIT_CODE_RE = re.compile(r"exit code[:\s]+([1-9]\d*)", re.IGNORECASE)

# Failure classes (from tool_diagnostics) that are DETERMINISTIC — a near-identical
# retry reproduces them, so they must not be looped on (#231). Distinct from
# change-and-retry classes (not_found, runtime_error) where a corrected retry can
# legitimately succeed. Two of these in a row already warrants a hard stop, below
# the generic fail_threshold.
_NON_RETRYABLE = frozenset({"timeout", "permission", "missing_command", "limit"})
_NONRETRY_THRESHOLD = 2

# Idempotent tools that are especially prone to content-free repetition and that
# the issue evidence shows spiraling with no progress even when individual calls
# return "success". Count these as non-progress after a shorter run so the model
# is nudged toward a different query / tool / strategy.
_SHORT_CIRCUIT_IDEMPOTENT = frozenset({"search_files", "web_search", "web_extract"})
_SHORT_CIRCUIT_REPEAT_THRESHOLD = 4

# Code-exploration tools whose mono-tool spiral is a STRATEGY problem, not a
# failure (#625): 16-17 consecutive read_file calls / 14 consecutive
# search_files calls, one file/query at a time, each succeeding — the agent
# just never reached for a cheaper bulk-overview alternative. Named here (not
# folded into the generic nudge text) so the suggestion only appears for the
# tools it actually applies to.
_EXPLORATION_ALTERNATIVE_HINT = {
    "read_file": (
        " For broad codebase exploration, call `repo_map` FIRST (if it is in your "
        "toolset); it gives a structured "
        "overview (functions/classes/methods with file:line) in a single call — "
        "far cheaper than reading files one at a time (Python codebases only). "
        "For a large batch of files you already know you need, `delegate_task` a "
        "subagent to read them and report back, keeping this context lean."
    ),
    "search_files": (
        " For broad codebase exploration, call `repo_map` FIRST (if it is in your "
        "toolset); it gives a structured "
        "overview (functions/classes/methods with file:line) in a single call — "
        "far cheaper than many narrow searches (Python codebases only). "
        "For a batch of searches you already know you need, `delegate_task` a "
        "subagent to run them and report back, keeping this context lean."
    ),
}

# Per-tool diversion advice appended to every nudge branch for the tools the
# introspection evidence shows spiraling (#694 family): navigation (#680),
# patch retries (#697), memory ops (#698), tool routing (#699), query
# reformulation (#700), and terminal diagnostics (#694). Exploration tools
# keep their dedicated #625 hints above (checked first in _diversion_hint).
_DIVERSION_HINT = {
    "terminal": (
        " Read the failing output above and act on its CONTENT (fix the code "
        "or the command); only if the failure looks environmental, verify "
        "cwd/env/prerequisites with one read-only check before another run."
    ),
    "patch": (
        " Anchor-not-found failures: re-read the exact target region with "
        "`read_file` — the file likely differs from what you assumed. Content "
        "errors after a successful match: fix the replacement text itself. "
        "Large rewrites: use `write_file`."
    ),
    "memory": (
        " Split the write into smaller entries or recall via `session_search` "
        "(if it is in your toolset) instead of retrying the identical memory "
        "operation."
    ),
    "tool_call": (
        " Verify the tool name and argument schema first (`tool_describe` / "
        "`skills_list`, if available); if an MCP server is unavailable, pick a "
        "native alternative instead of re-invoking it."
    ),
    "tool_describe": (
        " Verify the tool name first (`skills_list` or the available-tools "
        "list); if an MCP server is unavailable, pick a native alternative."
    ),
    "browser_navigate": (
        " Stop navigating: extract what you need from the CURRENT page "
        "(`browser_snapshot` for structure, `web_extract` for content — "
        "whichever is in your toolset) or change the information source "
        "entirely."
    ),
    "web_search": (
        " Stop reformulating queries: synthesize an answer from the results "
        "you already have, or `web_extract` (if available) the most promising "
        "hit for depth."
    ),
}


def _diversion_hint(tool: str) -> str:
    """Tool-specific redirect advice for a nudge; empty when none is defined."""
    return _EXPLORATION_ALTERNATIVE_HINT.get(tool) or _DIVERSION_HINT.get(tool, "")


def _category_hint(category: Optional[str]) -> Optional[str]:
    """tool_diagnostics recovery hint for a failure category (#365). Lazy
    import with a no-op fallback so loop_guard stays standalone."""
    if not category:
        return None
    try:
        from agent.tool_diagnostics import hint_for
    except Exception:  # pragma: no cover - keep standalone if import path differs
        return None
    return hint_for(category)

# Mutating tools get LOWER thresholds than idempotent tools because a fixation
# on mutating operations (writing files, running commands) is more costly and
# indicates a deeper strategy problem (#432).
_IDEMPOTENT_TOOLS = frozenset({
    "read_file",
    "search_files",
    "web_search",
    "web_extract",
    "session_search",
    "browser_snapshot",
    "browser_console",
    "browser_get_images",
    "mcp_filesystem_read_file",
    "mcp_filesystem_read_text_file",
    "mcp_filesystem_read_multiple_files",
    "mcp_filesystem_list_directory",
    "mcp_filesystem_list_directory_with_sizes",
    "mcp_filesystem_directory_tree",
    "mcp_filesystem_get_file_info",
    "mcp_filesystem_search_files",
})
_MUTATING_TOOLS = frozenset({
    "terminal",
    "execute_code",
    "write_file",
    "patch",
    "todo",
    "memory",
    "skill_manage",
    "browser_click",
    "browser_type",
    "browser_press",
    "browser_scroll",
    "browser_navigate",
    "send_message",
    "cronjob",
    "delegate_task",
    "process",
})
# Default thresholds: lower for mutating tools, higher for idempotent (#432).
# Mutating:  repeat at 4, fail at 2, escalate at 8
# Idempotent: repeat at 8, fail at 4, escalate at 15
_MUTATING_REPEAT_THRESHOLD = 4
_IDEMPOTENT_REPEAT_THRESHOLD = 8
_MUTATING_FAIL_THRESHOLD = 2
_IDEMPOTENT_FAIL_THRESHOLD = 4
_MUTATING_ESCALATE_THRESHOLD = 8
_IDEMPOTENT_ESCALATE_THRESHOLD = 15

# Unattended cron sessions get real enforcement, not just advisory text (#624):
# advisory nudges are routinely ignored by the model with no human present to
# course-correct (observed: 9 warnings ignored, 65 consecutive terminal calls).
# After this many nudges for the SAME stuck run, the cron turn ends as a
# failure instead of nudging again. Interactive surfaces are unaffected — this
# constant is only consulted when ``agent.platform == "cron"``.
CRON_LOOP_GUARD_HARD_STOP_THRESHOLD = 2


def should_cron_hard_stop(platform: Optional[str], warning_count: int) -> bool:
    """True when an unattended cron turn should end as a failure instead of
    nudging again (#624): only ever True for ``platform == "cron"`` once the
    same stuck run has already been nudged
    ``CRON_LOOP_GUARD_HARD_STOP_THRESHOLD`` times. Interactive surfaces
    (CLI/gateway/messaging) always return False — a human is present there to
    course-correct, so the guard stays purely advisory."""
    return platform == "cron" and warning_count >= CRON_LOOP_GUARD_HARD_STOP_THRESHOLD


def _failure_category(content: Any) -> Optional[str]:
    """The tool_diagnostics failure class of a result, or None if not a failure.
    Imported lazily with a no-op fallback so loop_guard stays standalone."""
    try:
        from agent.tool_diagnostics import classify
    except Exception:  # pragma: no cover - keep standalone if import path differs
        return None
    hit = classify(content)
    return hit[0] if hit else None


def _looks_like_failure(content: Any) -> bool:
    if not isinstance(content, str) or not content:
        return False
    low = content.lower()
    if any(m in low for m in _FAILURE_MARKERS):
        return True
    return bool(_EXIT_CODE_RE.search(content))


def _tool_call_arg_hash(tool_calls: List[Dict[str, Any]]) -> Optional[str]:
    """Canonical key of the INPUT arguments of an assistant turn's tool call(s).

    Used to detect identical-query repetition for spiral-prone idempotent tools
    like web_search / web_extract (#467): the same query produces the same
    non-progressing result and drives a loop.

    Identity is read from ``tool_calls[].function.arguments`` — the ACTUAL call
    inputs — NOT from the tool result. Tool results do not carry the input args,
    and web_search / web_extract outputs are XML-wrapped in
    ``<untrusted_tool_result>`` so they can never be parsed back into arguments;
    reading identity from the result left this short-circuit permanently inert.

    ``arguments`` may be a JSON string (the OpenAI wire format) or an already
    parsed dict; both normalize to the same canonical key, and key ordering is
    irrelevant. An unparseable string is hashed verbatim (still a stable
    identity). Returns None when NO arguments can be recovered from any call, so
    a turn with missing args never yields a false identity match (fail-safe: no
    spurious short-circuit).
    """
    keys: List[str] = []
    for tc in tool_calls:
        if not isinstance(tc, dict):
            continue
        fn = tc.get("function")
        if not isinstance(fn, dict):
            continue
        raw = fn.get("arguments")
        if raw is None:
            continue
        parsed: Any
        if isinstance(raw, str):
            s = raw.strip()
            if not s:
                continue
            try:
                parsed = json.loads(s)
            except Exception:
                keys.append(s)  # unparseable args: hash the raw string verbatim
                continue
        else:
            parsed = raw
        try:
            keys.append(
                json.dumps(
                    parsed, sort_keys=True, ensure_ascii=False, separators=(",", ":")
                )
            )
        except (TypeError, ValueError):
            keys.append(repr(parsed))
    if not keys:
        return None
    return "|".join(keys)


def _recent_tool_runs(
    messages: List[Dict[str, Any]],
) -> List[Tuple[str, bool, Optional[str], Optional[str]]]:
    """Most-recent-first list of
    (single_tool_name, result_failed, failure_class, arg_hash)
    for the trailing run of assistant turns that each called EXACTLY ONE tool.
    ``failure_class`` is the tool_diagnostics category of the failing result (or
    None when the turn did not fail). ``arg_hash`` is a canonical key of the
    assistant tool-call INPUT arguments for the turn (``function.arguments``),
    when they can be recovered.

    Stops at the first assistant turn that is not a single-tool call (a text
    reply, or a multi-tool turn) — that breaks the "stuck on one tool" run.
    Multi-tool turns are normal varied work, not a single-tool spiral.
    """
    runs: List[Tuple[str, bool, Optional[str], Optional[str]]] = []
    i = len(messages) - 1
    # Collect tool results by id as we walk back so we can mark failures.
    while i >= 0:
        msg = messages[i]
        if msg.get("role") == "assistant" and msg.get("tool_calls"):
            tcs = [tc for tc in msg["tool_calls"] if isinstance(tc, dict)]
            names = [
                tc.get("function", {}).get("name") for tc in tcs if tc.get("function")
            ]
            names = [n for n in names if n]
            if len(set(names)) != 1:
                break  # text turn or multi-tool turn — run ends
            tool = names[0]
            if runs and tool != runs[0][0]:
                break  # tool changed — the same-tool run ends here
            # Identity for #467 same-query detection comes from the call INPUT
            # args of THIS assistant turn, not the result that follows it.
            arg_hash = _tool_call_arg_hash(tcs)
            # Results for this turn are the "tool" messages that follow it.
            failed = False
            category: Optional[str] = None
            for j in range(i + 1, len(messages)):
                tm = messages[j]
                if tm.get("role") != "tool":
                    break
                content = tm.get("content")
                if _looks_like_failure(content):
                    failed = True
                    category = _failure_category(content) or category
            runs.append((tool, failed, category, arg_hash))
            i -= 1
        elif msg.get("role") == "tool":
            i -= 1  # skip result messages; handled with their assistant turn
        else:
            break  # user/system/text-assistant turn breaks the run
    return runs


def _tool_category(tool_name: str) -> str:
    """Return 'mutating', 'idempotent', or 'unknown' for a tool name."""
    if tool_name in _MUTATING_TOOLS:
        return "mutating"
    if tool_name in _IDEMPOTENT_TOOLS:
        return "idempotent"
    return "unknown"


def _tool_spiral_score(tool_name: str, count: int, base: int) -> Optional[str]:
    """Compute a diversity-awareness score for the nudge message.

    Returns a one-line annotation like 'spiral-index: 5' when the number of
    consecutive calls is meaningfully above the base threshold, or None for
    short runs.
    """
    if count <= base:
        return None
    excess = count - base
    intensity = min(excess // 2, 5)  # cap at 5 for readability
    if intensity >= 2:
        return f"spiral-intensity: {intensity} of 5"
    return None


def maybe_nudge(
    messages: List[Dict[str, Any]],
    *,
    repeat_threshold: Optional[int] = None,
    fail_threshold: Optional[int] = None,
) -> Optional[str]:
    """Return a nudge string if the trailing single-tool run is stuck, else None.

    Trigger levels (each is lower for mutating tools than idempotent):
      1. Non-retryable failure class repeated twice (highest priority, #231)
      2. Generic failures >= fail_threshold
      3. Same tool called >= repeat_threshold times in a row
      4. Escalated interrupt at higher counts (#432)
      5. Same *arguments* repeated for short-circuit idempotent tools
         (search_files / web_search / web_extract) >= 4 times (#467)

    Returns None when the agent is making varied progress (not stuck).
    """
    runs = _recent_tool_runs(messages)
    if not runs:
        return None
    tool = runs[0][0]

    # Pick thresholds based on tool category (#432).
    # Unknown tools get mutating thresholds as the safer default.
    cat = _tool_category(tool)
    is_mutating = cat == "mutating"
    is_unknown = cat == "unknown"
    if repeat_threshold is None:
        repeat_threshold = (
            _MUTATING_REPEAT_THRESHOLD
            if (is_mutating or is_unknown)
            else _IDEMPOTENT_REPEAT_THRESHOLD
        )
    if fail_threshold is None:
        fail_threshold = (
            _MUTATING_FAIL_THRESHOLD
            if (is_mutating or is_unknown)
            else _IDEMPOTENT_FAIL_THRESHOLD
        )
    escalate_threshold = (
        _MUTATING_ESCALATE_THRESHOLD
        if (is_mutating or is_unknown)
        else _IDEMPOTENT_ESCALATE_THRESHOLD
    )
    short_circuit_threshold = (
        _MUTATING_REPEAT_THRESHOLD
        if (is_mutating or is_unknown)
        else (
            _SHORT_CIRCUIT_REPEAT_THRESHOLD
            if tool in _SHORT_CIRCUIT_IDEMPOTENT
            else repeat_threshold
        )
    )

    # All entries in `runs` share the same tool (run breaks on tool change),
    # but guard anyway:
    same = [r for r in runs if r[0] == tool]
    count = len(same)
    consec_fail = 0
    consec_nonretry = 0
    nonretry_class: Optional[str] = None
    counting_nonretry = True
    for _t, failed, category, _arg_hash in same:
        if failed:
            consec_fail += 1
        else:
            break
        # Trailing run of failures that are all the SAME deterministic class.
        if counting_nonretry and category in _NON_RETRYABLE:
            if nonretry_class is None or category == nonretry_class:
                nonretry_class = category
                consec_nonretry += 1
            else:
                counting_nonretry = False
        else:
            counting_nonretry = False

    # Category label for nudge messages.
    if is_mutating:
        cat_label = "mutating"
    elif is_unknown:
        cat_label = "unknown"
    else:
        cat_label = "idempotent"

    # Highest-priority: a DETERMINISTIC failure repeated even once (#231). These
    # reproduce on a near-identical retry, so the generic 3-strike threshold is
    # too lenient — two in a row is already a spiral (terminal timeouts, denied
    # paths, missing binaries, size-limit caps). Stop hard and name the class.
    if consec_nonretry >= _NONRETRY_THRESHOLD:
        return (
            f"[loop-guard] `{tool}` returned a non-retryable `{nonretry_class}` "
            f"failure {consec_nonretry} times in a row. This class is DETERMINISTIC "
            f"— the same call reproduces it, so retrying is futile. Do NOT call "
            f"`{tool}` the same way again. Change the approach now: adjust the "
            f"parameters/path/command, route to a fallback tool, or report the "
            f"blocker concisely if it can't be resolved."
            f"{_diversion_hint(tool)}"
        )

    if consec_fail >= fail_threshold:
        # Name the most frequent failure class among the trailing failures and
        # surface its recovery hint (#365) so the model reacts to WHAT is
        # failing, not just that it failed. Ties resolve to the most recent
        # class (same is most-recent-first).
        _fail_classes = [
            c for _t, failed, c, _a in same[:consec_fail] if failed and c
        ]
        dominant = None
        if _fail_classes:
            _counts: Dict[str, int] = {}
            for c in _fail_classes:
                _counts[c] = _counts.get(c, 0) + 1
            dominant = max(_counts, key=lambda k: (_counts[k], -_fail_classes.index(k)))
        class_note = ""
        if dominant:
            _hint = _category_hint(dominant)
            class_note = f" Most frequent error class: `{dominant}`." + (
                f" {_hint}" if _hint else ""
            )
        return (
            f"[loop-guard] The `{tool}` tool ({cat_label}) has failed "
            f"{consec_fail} times in a row with the same approach. STOP repeating "
            f"it. Diagnose the actual blocker first (check prerequisites / "
            f"environment / the exact error class), then either switch to a "
            f"different tool or strategy, or — if the blocker can't be resolved "
            f"— report it concisely instead of retrying. Do not call `{tool}` "
            f"again the same way."
            f"{class_note}"
            f"{_diversion_hint(tool)}"
        )

    # Same-argument repetition for known spiral-prone idempotent tools (#467).
    # This catches web_search returning "no results" / search_files returning
    # nothing, where each individual call technically "succeeded" but repeating
    # the exact same query is still a loop.
    if tool in _SHORT_CIRCUIT_IDEMPOTENT and count >= short_circuit_threshold:
        arg_hashes = [r[3] for r in same if r[3] is not None]
        if arg_hashes and len(set(arg_hashes)) == 1:
            score = _tool_spiral_score(tool, count, short_circuit_threshold)
            score_line = f"\n{score}" if score else ""
            return (
                f"[loop-guard] You have called `{tool}` {count} times with the "
                f"SAME arguments and the result is not making progress.{score_line} "
                f"Do NOT repeat `{tool}` with those identical arguments. Rephrase "
                f"the query, broaden or narrow it, switch to a different information "
                f"source, or state the blocker if no relevant results are available."
                # Deliberately NO appended hints here: this branch's advice is
                # already specific to identical-argument repetition, and e.g.
                # web_search's 'stop reformulating' diversion would directly
                # contradict the 'rephrase the query' instruction above
                # (consult review). The #625 exploration hints are excluded by
                # the same long-standing design decision.
            )

    if count >= repeat_threshold:
        # Build diversity score for the nudge.
        score = _tool_spiral_score(tool, count, repeat_threshold)
        score_line = f"\n{score}" if score else ""
        exploration_hint = _diversion_hint(tool)

        if count >= escalate_threshold:
            return (
                f"[loop-guard] You have called `{tool}` ({cat_label}) {count} "
                f"times in a row without resolving the task.{score_line}\n"
                f"⚠️  ESCALATED INTERRUPT: This is a deep mono-tool spiral. "
                f"PAUSE and summarize in one paragraph the concrete progress "
                f"these {count} calls have made toward the goal. If no measurable "
                f"progress exists, state the actual blocker explicitly and "
                f"propose a fundamentally different strategy — do NOT call "
                f"`{tool}` again until you have provided this summary."
                f"{exploration_hint}"
            )

        return (
            f"[loop-guard] You have called `{tool}` ({cat_label}) {count} times "
            f"in a row without resolving the task.{score_line} Pause and re-read "
            f"the goal: what concrete progress have these calls made? Check your "
            f"plan/success criterion, then either change strategy, move to the "
            f"next step, or report the blocker. Avoid another near-identical "
            f"`{tool}` call."
            f"{exploration_hint}"
        )

    return None


def current_run_signature(messages: List[Dict[str, Any]]) -> Optional[Tuple[str, int]]:
    """(tool, count) of the trailing single-tool run, or None. Callers use this
    to nudge once per escalating run instead of every iteration."""
    runs = _recent_tool_runs(messages)
    if not runs:
        return None
    return (runs[0][0], len(runs))
