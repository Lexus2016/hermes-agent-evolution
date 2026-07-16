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

Refusal detection (#975): assistant text responses containing refusal language
("I can't", "I don't have access", "I'm unable to") are classified via a
lightweight taxonomy and a recovery directive is returned.  The caller injects
it as a user-role nudge so the model gets one chance to course-correct before
the refusal is accepted as the final answer.  Mirrors the tool-spiral nudge
pattern but for text-only responses.
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
# File-read tools: re-reading the SAME path+offset+limit returns content the
# model already has, so an identical-argument repeat is a spiral just like a
# repeated search query.  read_file is the second-largest retry-spiral cluster
# in the agent's own traces (up to 8 consecutive identical calls, #1092), so
# trip the same-argument short-circuit at _SHORT_CIRCUIT_REPEAT_THRESHOLD (4)
# instead of waiting for the generic idempotent repeat_threshold (8) — half the
# wasted calls / prompt-cache budget.  Kept as a distinct set so the nudge can
# speak in file terms (offset/limit) rather than the search "rephrase the query"
# wording, which does not apply to a file read.
_SHORT_CIRCUIT_FILE_READ = frozenset({"read_file", "mcp_filesystem_read_file"})
_SHORT_CIRCUIT_IDEMPOTENT = _SHORT_CIRCUIT_IDEMPOTENT | _SHORT_CIRCUIT_FILE_READ
_SHORT_CIRCUIT_REPEAT_THRESHOLD = 4

# #1012 — web_search with *different* queries is a distinct spiral from the
# same-argument loop (#467): the model reformulates the query each time, each
# call technically "succeeds", but it never stops to synthesize the results
# into an answer. The generic repeat_threshold (8 for idempotent tools) is too
# high — by then the agent has wasted 8+ iterations. This cap triggers earlier,
# at a count where the model should already have enough results to synthesize.
# The nudge explicitly tells it to stop searching and write the answer.
_WEB_SEARCH_DIVERSE_QUERY_CAP = 6

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
        "For several files you already know you need, pass a list of up to 10 "
        "paths to `read_file` in one call instead of reading them sequentially. "
        "For a large batch beyond that, `delegate_task` a "
        "subagent to read them and report back, keeping this context lean."
    ),
    "search_files": (
        " For broad codebase exploration, call `repo_map` FIRST (if it is in your "
        "toolset); it gives a structured "
        "overview (functions/classes/methods with file:line) in a single call — "
        "far cheaper than many narrow searches (Python codebases only). "
        "For a batch of searches you already know you need, `delegate_task` a "
        "subagent to run them and report back, keeping this context lean."
        # #973 — regex/glob parse errors are deterministic: a near-identical
        # retry reproduces them. Route to repo_map or read_file instead of
        # re-issuing the same failing pattern.
        " If search_files is returning regex/glob parse errors, the pattern is "
        "likely invalid — use `search_files target='files'` with a glob pattern "
        "instead of a content regex, or switch to `repo_map` for a structured "
        "overview."
    ),
}

# Per-tool diversion advice appended to every nudge branch for the tools the
# introspection evidence shows spiraling (#694 family): navigation (#680),
# patch retries (#697), memory ops (#698), tool routing (#699), query
# reformulation (#700), terminal diagnostics (#694), and media generation /
# analysis (#739). Exploration tools keep their dedicated #625 hints above
# (checked first in _diversion_hint).
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
    "vision_analyze": (
        " Stop re-calling: confirm the image path exists and the format is "
        "supported (png/jpg/webp) with `read_file`. If the input is invalid or "
        "the tool is unavailable, describe the visual from surrounding context "
        "and report the blocker instead of retrying the same analysis."
    ),
    "image_generate": (
        " Stop re-generating: verify the prompt and that an image provider is "
        "configured. If generation keeps failing, deliver a text "
        "description/placeholder and report the visual blocker rather than "
        "looping on the same call."
    ),
    "video_analyze": (
        " Stop re-calling: confirm the video path and format with `read_file`. "
        "If the input is invalid or the tool is unavailable, work from a text "
        "summary and report the blocker instead of retrying."
    ),
    "video_generate": (
        " Stop re-generating: verify the prompt and that a video provider is "
        "configured. If generation keeps failing, deliver a text placeholder "
        "and report the visual blocker rather than looping."
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

# #973 — Per-tool fail threshold overrides. ``search_files`` failures are
# typically regex/glob parse errors — deterministic and cheap to route
# around (switch to repo_map or read_file). Trip one call sooner than the
# generic idempotent threshold so the agent gets the diversion hint before
# burning another iteration on the same broken pattern.
_TOOL_FAIL_THRESHOLD_OVERRIDE: dict[str, int] = {
    "search_files": 3,
}

# Monitoring/polling tools whose whole PURPOSE is to be called repeatedly to
# observe a long-running background job (CI runs, `hermes update`, a spawned
# process). Re-checking the same handle and getting the same "still running"
# answer is legitimate WAITING, not a spiral, so these are exempt from the cron
# HARD STOP below (they still receive purely-advisory nudges, and a genuinely
# FAILING poll is still caught by the failure branch). Without this, the
# evolution-integration job — which polls a background process while it waits
# for a PR's CI to settle — was hard-stopped and marked failed on essentially
# every run (see run_warrants_cron_hard_stop).
_POLLING_TOOLS = frozenset({"process"})

# Unattended cron sessions get real enforcement, not just advisory text (#624):
# advisory nudges are routinely ignored by the model with no human present to
# course-correct (observed: 9 warnings ignored, 65 consecutive terminal calls).
# After this many nudges for the SAME stuck run, the cron turn ends as a
# failure instead of nudging again. Interactive surfaces are unaffected — this
# constant is only consulted when ``agent.platform == "cron"``.
CRON_LOOP_GUARD_HARD_STOP_THRESHOLD = 2

# Interactive escalation (#1109, #1110, #1111, #1112): advisory nudges are
# also routinely ignored on INTERACTIVE surfaces when the spiral is a genuine
# failing one (``run_warrants_cron_hard_stop`` is True — the tool is actually
# failing or stuck in an identical-call loop, not just doing repetitive but
# successful work). Observed: terminal retry spirals reaching 29 consecutive
# calls (#1109), execute_code at 17 (#1110), browser_navigate at 21 (#1111),
# read_file at 8 (#1112). A human is present but the spiral burns context and
# budget with no benefit. After this many warnings for the SAME stuck run —
# deliberately HIGHER than the cron threshold (2) to give a human more room to
# intervene — also end the interactive turn as a failure for genuine spirals.
# Legitimate mono-tool runs that merely LOOK repetitive (distinct successful
# calls) never reach this gate because ``run_warrants_cron_hard_stop`` returns
# False for them, so this only ever stops genuine non-progress.
INTERACTIVE_LOOP_GUARD_HARD_STOP_THRESHOLD = 4


def should_cron_hard_stop(platform: Optional[str], warning_count: int) -> bool:
    """True when an unattended cron turn should end as a failure instead of
    nudging again (#624): only ever True for ``platform == "cron"`` once the
    same stuck run has already been nudged
    ``CRON_LOOP_GUARD_HARD_STOP_THRESHOLD`` times. Interactive surfaces
    (CLI/gateway/messaging) always return False — a human is present there to
    course-correct, so the guard stays purely advisory."""
    return platform == "cron" and warning_count >= CRON_LOOP_GUARD_HARD_STOP_THRESHOLD


def should_interactive_hard_stop(
    platform: Optional[str],
    warning_count: int,
    genuine_spiral: bool,
) -> bool:
    """True when an INTERACTIVE (non-cron) turn should end as a failure for a
    genuine failing spiral that has ignored ``INTERACTIVE_LOOP_GUARD_HARD_STOP_THRESHOLD``
    advisory warnings (#1109–#1112).

    Only fires when ALL of the following hold:
      * ``platform`` is NOT ``"cron"`` (cron uses its own lower threshold).
      * ``genuine_spiral`` is True — i.e. ``run_warrants_cron_hard_stop`` says
        the trailing run is genuinely flailing (failing repeatedly or stuck in
        an identical-call loop), NOT merely doing repetitive-but-successful
        mono-tool work.
      * ``warning_count >= INTERACTIVE_LOOP_GUARD_HARD_STOP_THRESHOLD`` — the
        model has been given several escalating chances to course-correct.

    This ensures interactive spirals that burn context with no progress are
    stopped, while preserving the purely-advisory behaviour for legitimate
    repetitive work (the iteration/budget guard remains the backstop there).
    """
    if platform == "cron":
        return False  # cron has its own path via should_cron_hard_stop
    if not genuine_spiral:
        return False  # repetitive-but-successful work: keep nudging
    return warning_count >= INTERACTIVE_LOOP_GUARD_HARD_STOP_THRESHOLD


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
        # #973 — per-tool override (e.g. search_files trips sooner).
        fail_threshold = _TOOL_FAIL_THRESHOLD_OVERRIDE.get(tool, fail_threshold)
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
            _is_file_read = tool in _SHORT_CIRCUIT_FILE_READ
            # File-read tools defer an extreme identical-read spiral
            # (>= escalate_threshold) to the stronger escalation interrupt
            # below; the debounce owns the earlier
            # [short_circuit_threshold, escalate_threshold) window. Search tools
            # have no escalation tie-in, so they keep short-circuiting at any
            # count above the threshold. (#1092)
            if not (_is_file_read and count >= escalate_threshold):
                score = _tool_spiral_score(tool, count, short_circuit_threshold)
                score_line = f"\n{score}" if score else ""
                if _is_file_read:
                    return (
                        f"[loop-guard] You have called `{tool}` {count} times with the "
                        f"SAME path/offset/limit — you already have that file content "
                        f"and re-reading returns the same bytes.{score_line} Do NOT "
                        f"re-read the identical range. Use the content you already "
                        f"have, read a DIFFERENT part of the file (change offset/limit) "
                        f"or a different file, or state the blocker if the file does "
                        f"not contain what you need."
                        # Unlike the search branch below, the #625 exploration
                        # hint (repo_map / bulk read / delegate_task) does NOT
                        # contradict "don't re-read the identical range" — it is
                        # the complementary better strategy, so keep it.
                        f"{_diversion_hint(tool)}"
                    )
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

    # #1012 — web_search diverse-query spiral: the model keeps reformulating
    # the query (different args each time, each call "succeeds") but never
    # stops to synthesize an answer. The same-argument guard above does NOT
    # catch this because the args differ. This cap fires at a lower count than
    # the generic repeat_threshold, with a nudge that explicitly directs
    # synthesis from the results already gathered.
    if (
        tool == "web_search"
        and count >= _WEB_SEARCH_DIVERSE_QUERY_CAP
    ):
        arg_hashes = [r[3] for r in same if r[3] is not None]
        # Only trigger when queries are genuinely diverse (not all identical —
        # that's already handled by the same-arg branch above).
        if arg_hashes and len(set(arg_hashes)) >= 2:
            return (
                f"[loop-guard] You have called `web_search` {count} times with "
                f"different queries. You have enough information — STOP searching "
                f"and synthesize an answer from the results you already have. "
                f"If the results are insufficient, use `web_extract` on the most "
                f"promising URL for depth, or report what you found and what is "
                f"still missing instead of running another search."
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


def run_warrants_cron_hard_stop(messages: List[Dict[str, Any]]) -> bool:
    """Second gate on the #624 cron hard stop: end the turn as a failure ONLY
    when the trailing single-tool run is genuinely FLAILING (no progress), never
    when it is doing legitimate mono-tool work that merely LOOKS repetitive.

    The advisory-nudge counter alone over-triggers: the evolution
    implementation and integration jobs were hard-stopped and marked failed on
    essentially every run because their legitimate core workflow IS a burst of
    consecutive same-tool calls —

      * implementation: a run of DISTINCT, successful `terminal` calls to
        finalize a change (lint -> format -> git add -> commit -> push ->
        gh pr create). Distinct successful commands are real progress, not a
        spiral.
      * integration: repeated `process` polls while WAITING for a PR's CI to
        settle. Re-checking a background job is that tool's purpose, not a loop.

    So a hard stop is warranted only for genuine non-progress:

      1. A deterministic non-retryable failure repeated (`_NON_RETRYABLE`).
      2. The tool FAILING `fail_threshold`+ times in a row (the real #624 case:
         terminal timeouts / denied paths retried unchanged).
      3. A non-monitoring tool called `>= 2` times with the EXACT same arguments
         and no failure — a degenerate identical-call loop (`echo hi` x N).
         Deliberately strict (not a diversity ratio): cyclic INSPECTION with
         varied args (git status/diff across files) is legitimate progress.

    A trailing run of DISTINCT, successful calls (real progress) or any polling
    of a `_POLLING_TOOLS` handle returns False here: keep nudging advisorily,
    but do NOT kill the run. The iteration/budget guard remains the backstop
    against a truly unbounded distinct-call loop.
    """
    runs = _recent_tool_runs(messages)
    if not runs:
        return False
    tool = runs[0][0]
    same = [r for r in runs if r[0] == tool]

    # Trailing consecutive failures (most-recent-first), tracking a run of the
    # SAME deterministic non-retryable class — mirrors maybe_nudge's counting.
    consec_fail = 0
    consec_nonretry = 0
    nonretry_class: Optional[str] = None
    counting_nonretry = True
    for _t, failed, category, _arg_hash in same:
        if failed:
            consec_fail += 1
        else:
            break
        if counting_nonretry and category in _NON_RETRYABLE:
            if nonretry_class is None or category == nonretry_class:
                nonretry_class = category
                consec_nonretry += 1
            else:
                counting_nonretry = False
        else:
            counting_nonretry = False

    cat = _tool_category(tool)
    is_mutating_or_unknown = cat in ("mutating", "unknown")
    fail_threshold = (
        _MUTATING_FAIL_THRESHOLD
        if is_mutating_or_unknown
        else _IDEMPOTENT_FAIL_THRESHOLD
    )

    # (1) + (2): genuine failing spiral — applies to every tool, including
    # polling tools (a `process` that keeps ERRORING is still a real problem).
    if consec_nonretry >= _NONRETRY_THRESHOLD:
        return True
    if consec_fail >= fail_threshold:
        return True

    # Polling/monitoring tools: repeated SUCCESSFUL identical polls are
    # legitimate waiting, not a loop. Only the failure branch above can stop
    # them. (Observed in prod: the integration job uses 60s BLOCKING waits, so
    # a tight budget-burn is not the real pattern; the iteration budget is the
    # backstop for a pathological no-sleep poller.)
    if tool in _POLLING_TOOLS:
        return False

    # (3): degenerate EXACT-identical repetition with no failure — the same call
    # producing the same non-progressing result (the canonical #624 `echo hi`
    # x N loop). Deliberately STRICT (all args identical), not a diversity
    # ratio: a cross-provider review (Kimi) showed that any "unique <= calls/2"
    # style ratio false-positives on legitimate cyclic INSPECTION — e.g. a merge
    # resolution doing `git status` -> `git diff A` -> `git diff B` -> `git
    # status` ... (3 unique over 6 calls) is real progress with intervening
    # state changes, not a spiral. Since the whole point of this gate is to stop
    # KILLING legitimate work, we err toward the conservative check: a genuine
    # oscillation that this misses merely wastes one run's budget (the iteration
    # budget backstops it), whereas a false positive re-creates the exact
    # daily-failure pain we are fixing. Distinct commands == real progress.
    arg_hashes = [r[3] for r in same if r[3] is not None]
    if len(same) >= 2 and arg_hashes and len(set(arg_hashes)) == 1:
        return True

    return False


# ── Refusal taxonomy and recovery (#975) ──────────────────────────────
# Lightweight inline taxonomy — mirrors scripts/evolution_refusal_taxonomy.py
# but lives in the agent core so the conversation loop can call it without a
# subprocess.  Classifies refusal language in assistant text responses and
# returns a recovery directive the caller injects as a user-role nudge.

_REFUSAL_PAT = re.compile(
    "|".join([  # fmt: skip
        r"i can'?t",
        r"i can not",
        r"i don'?t have (?:access|permission)",
        r"no access",
        r"i'?m unable to",
        r"i don'?t have (?:a |the )?(?:tool|skill|feature|plugin|ability|capability)",
        r"i cannot (?:help|assist|do|provide|access)",
        r"not (?:able|allowed) to",
    ]),
    re.IGNORECASE,
)
# Rhetorical false positives — "I can't stress this enough" etc.
_FP_REFUSAL_PAT = re.compile(
    r"i can'?t (?:stress|emphasize|overstate|imagine|believe|say|thank|praise|wait)",
    re.IGNORECASE,
)

_REFUSAL_CATEGORIES = {
    "true_capability_gap": (
        r"don'?t have (?:a |the )?(?:tool|skill|plugin|feature)",
        "A capability gap was cited. Check whether the capability exists "
        "locally (use `hermes tools` or check skills) before accepting the "
        "refusal. If a tool/skill is available, use it directly. If genuinely "
        "missing, suggest how to install or configure it rather than stopping.",
    ),
    "permission_boundary": (
        r"permission|security|unauthorized|forbidden",
        "A permission/security boundary was cited. Verify this is a genuine "
        "security boundary and not an over-refusal — check whether the "
        "action is safe and permitted in the current context. If legitimate, "
        "explain the boundary and suggest an alternative approach.",
    ),
    "over_refusal": (
        r"",
        "The capability likely exists locally. Before accepting this refusal, "
        "re-check available tools and skills — the requested action may be "
        "achievable with the tools already configured. Use them directly.",
    ),
}


def _classify_refusal(text: str) -> str:
    """Classify a refusal snippet into a category, or empty string if spurious."""
    # Check false-positive pattern first
    if _FP_REFUSAL_PAT.search(text):
        return ""
    # Check each category in priority order
    for cat, (pat, _) in _REFUSAL_CATEGORIES.items():
        if pat and re.search(pat, text, re.IGNORECASE):
            return cat
    return "over_refusal"


def maybe_refusal_nudge(
    messages: List[Dict[str, Any]],
    *,
    already_nudged: bool = False,
) -> Optional[str]:
    """Return a recovery directive if the last assistant message contains
    refusal language, else None.

    Called from the conversation loop when the assistant produces a text-only
    response (no tool calls).  If the text matches refusal patterns, a
    taxonomy-specific recovery nudge is returned for injection as a user
    message — giving the model one chance to course-correct before the
    refusal is accepted as the final answer.

    ``already_nudged`` prevents double-nudging the same refusal (the caller
    tracks this, mirroring the tool-spiral nudge pattern).
    """
    if already_nudged:
        return None
    # Find the last assistant message with text content
    last_assistant_text = None
    for msg in reversed(messages):
        if not isinstance(msg, dict):
            continue
        if msg.get("role") != "assistant":
            continue
        content = msg.get("content", "")
        if not isinstance(content, str) or not content.strip():
            continue
        # Skip synthetic/recovery messages
        if msg.get("_empty_terminal_sentinel") or msg.get("_thinking_prefill"):
            continue
        last_assistant_text = content
        break
    if not last_assistant_text:
        return None
    # Detect refusal language
    refusals = [
        m for m in _REFUSAL_PAT.finditer(last_assistant_text)
        if not _FP_REFUSAL_PAT.match(last_assistant_text[m.start():m.start() + 40])
    ]
    if not refusals:
        return None
    # Classify using the context around the first refusal
    first = refusals[0]
    snippet = last_assistant_text[first.start():first.start() + 120]
    category = _classify_refusal(snippet)
    if not category:
        return None
    # Build recovery directive
    _, recovery = _REFUSAL_CATEGORIES.get(category, _REFUSAL_CATEGORIES["over_refusal"])
    return (
        f"[loop-guard] Refusal detected ({category}). {recovery} "
        f"Do not simply repeat the refusal — take concrete action or "
        f"explain specifically what is needed to proceed."
    )
