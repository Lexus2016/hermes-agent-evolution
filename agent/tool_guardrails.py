"""Pure tool-call loop guardrail primitives.

The controller in this module is intentionally side-effect free: it tracks
per-turn tool-call observations and returns decisions. Runtime code owns whether
those decisions become warning guidance, synthetic tool results, or controlled
turn halts.
"""

from __future__ import annotations

import hashlib
import json
from collections import deque
from dataclasses import asdict, dataclass, field, fields
from typing import Any, Mapping

from utils import safe_json_loads
from agent.tool_result_classification import (
    shape_failure_error, file_mutation_result_landed, has_failure_marker, is_guardrail_refusal, is_rate_limited)


IDEMPOTENT_TOOL_NAMES = frozenset({
    "read_file", "search_files", "web_search", "web_extract", "session_search", "skill_view", "skills_list",
    "browser_snapshot", "browser_console", "browser_get_images", "mcp_filesystem_read_file",
    "mcp_filesystem_read_text_file", "mcp_filesystem_read_multiple_files", "mcp_filesystem_list_directory",
    "mcp_filesystem_list_directory_with_sizes", "mcp_filesystem_directory_tree", "mcp_filesystem_get_file_info",
    "mcp_filesystem_search_files",
})

MUTATING_TOOL_NAMES = frozenset({
    "terminal", "execute_code", "write_file", "patch", "todo_list", "memory", "skill_manage",
    "browser_click", "browser_type", "browser_press", "browser_scroll", "browser_navigate",
    "send_message", "cronjob_manage", "delegate_task", "process_manage",
})

# Pollers: legitimately re-invoked with identical args; the identical-call NOTICE never fires.
STALL_GUARD_REPEATABLE_TOOLS = frozenset({"process_manage"})
_STALL_GUARD_REPEATABLE_SUFFIXES = ("_get_result", "_poll")  # generated / MCP poller conventions
# Nth consecutive identical (tool, args, result) call that fires the notice; 3 tolerates one double-check.
STALL_GUARD_IDENTICAL_CALL_THRESHOLD = 3
# Repeating multi-call cycles (A,B,A,B,... with identical args AND results) defeat the
# consecutive streak above — every alternation resets it, so a model replaying the same
# 2–4 call batch each iteration ran to the budget unflagged (port of can1357/oh-my-pi#10521,
# which widened their loop guard from single-call turns to whole tool-call batches).
# Longest cycle period detected; laps reuse the streak thresholds (notice at
# STALL_GUARD_IDENTICAL_CALL_THRESHOLD laps, halt at no_progress_block_after laps).
_STALL_GUARD_MAX_CYCLE_PERIOD = 4
# History window: enough for block_after laps of the longest cycle plus slack.
_STALL_GUARD_CYCLE_HISTORY = 64
# From the 2nd byte-identical repeat the duplicate payload becomes a reference stub; smaller results
# aren't worth it, errors never are. The args preview keeps WHAT was called if compression evicts the original.
IDENTICAL_RESULT_STUB_MIN_CHARS = 512

# How much of the canonical args JSON the stub carries so the model still
# knows WHAT the referenced call was even if context compression later
# evicts the referenced result (cheap dangling-reference mitigation).
_RESULT_STUB_ARGS_PREVIEW_CHARS = 120


# Tools whose "failure" is a normal, informative outcome of legitimate work:
# a red test run, a grep with no matches, a failing build during a fix loop, a
# page that times out. Hard stops never fire on these from failure counts of
# DIFFERENT commands (same_tool_failure) — only an exact-args replay with NO
# intervening change, or an identical-result streak, can halt them.
FAILURE_TOLERANT_TOOL_NAMES = frozenset(
    {
        "terminal",
        "execute_code",
        "process_manage",
        "process",
        "browser_navigate",
        "web_extract",
    }
)

# A landed mutation between two attempts means the retry is a NEW experiment
# (edit -> re-run) rather than a replay. A successful call to one of these
# marks progress for every failing signature still being counted this turn.
PROGRESS_RESET_TOOL_NAMES = frozenset(
    {
        "write_file",
        "patch",
        "terminal",
        "execute_code",
        "browser_click",
        "browser_type",
        "browser_press",
        "browser_navigate",
        "process_manage",
        "process",
        "delegate_task",
        "send_message",
        "cronjob",
        "cronjob_manage",
        "todo",
        "todo_list",
        "memory",
        "skill_manage",
    }
)


def is_stall_guard_repeatable(tool_name: str) -> bool:
    """Whether a tool is exempt from the identical-call loop notice."""
    return tool_name in STALL_GUARD_REPEATABLE_TOOLS or tool_name.endswith(_STALL_GUARD_REPEATABLE_SUFFIXES)


# Default session-wide caps, matching Claude Code's v2.1.212 runaway-loop
# Per-turn (per-agent-loop) caps on runaway-prone tool calls. Counts reset at
# the start of every agent loop (reset_for_turn), so the limit is "within a
# single turn" rather than cumulative over the whole session. A single loop
# issuing dozens of web searches or spawning dozens of subagents is already
# pathological, so the defaults are deliberately low.
_DEFAULT_MAX_WEB_SEARCHES_PER_TURN = 50
_DEFAULT_MAX_SUBAGENTS_PER_TURN = 50


@dataclass(frozen=True)
class LoopCapConfig:
    """Per-turn caps on runaway-prone tool calls.

    Inspired by Claude Code v2.1.212 (Week 29, July 2026), which added caps on
    WebSearch calls and subagent spawns to stop runaway search / delegation
    loops. Here the caps count *within a single agent loop* (one turn): the
    counters reset in ``reset_for_turn`` at the start of every
    ``run_conversation``, so a legitimate multi-turn session is never starved,
    but a single turn that spirals into an unbounded search / delegation loop
    is stopped.

    Semantics differ from the per-turn loop *detector* above (which keys on
    repeated identical/failing calls): these caps are a hard ceiling on the
    total count of a tool within the turn and fire regardless of
    ``hard_stop_enabled``. A value of ``0`` disables the cap (unlimited).
    """

    max_web_searches: int = _DEFAULT_MAX_WEB_SEARCHES_PER_TURN
    max_subagents: int = _DEFAULT_MAX_SUBAGENTS_PER_TURN

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any] | None) -> "LoopCapConfig":
        """Build config from the ``tool_loop_guardrails.loop_caps`` section."""
        if not isinstance(data, Mapping):
            return cls()
        defaults = cls()
        return cls(
            max_web_searches=_non_negative_int(
                data.get("max_web_searches"), defaults.max_web_searches
            ),
            max_subagents=_non_negative_int(
                data.get("max_subagents"), defaults.max_subagents
            ),
        )


_INTERACTIVE_PLATFORMS = frozenset({"cli", "tui", "desktop", "acp"})

# Platforms that are not chat gateways but whose work is a bounded, supervised
# task loop: a subagent inherits its parent's budget and is stopped by the
# parent; api_server runs have a live client holding the request. Both do
# real edit -> re-run work, so they keep the interactive (warn-only) default.
_SUPERVISED_TASK_PLATFORMS = frozenset({"subagent", "api_server"})


def _is_non_interactive_platform(platform: str | None) -> bool:
    """Return true for gateway/cron sessions where tool loops are unattended."""
    if not isinstance(platform, str) or not platform.strip():
        return False
    key = platform.strip().lower()
    if key in _INTERACTIVE_PLATFORMS or key in _SUPERVISED_TASK_PLATFORMS:
        return False
    return True


@dataclass(frozen=True)
class ToolCallGuardrailConfig:
    """Thresholds for per-turn tool-call loop detection.

    Warnings are enabled by default and never prevent tool execution. Hard stops
    stay opt-in for interactive CLI/TUI/Desktop/ACP sessions, but default on for
    non-interactive gateway/cron platforms where nobody is present to interrupt
    a model that ignores loop warnings.
    """

    warnings_enabled: bool = True
    hard_stop_enabled: bool = False
    non_interactive_hard_stop_enabled: bool = True
    exact_failure_warn_after: int = 2
    exact_failure_block_after: int = 5
    same_tool_failure_warn_after: int = 3
    same_tool_failure_halt_after: int = 8
    no_progress_warn_after: int = 2
    no_progress_block_after: int = 5
    # #745 — browser tools spiral expensively (each call drives a real browser)
    # and their deterministic failures (CDP down, nav timeout, missing tool) do
    # not recover on a blind retry. Cap consecutive same-browser-tool failures
    # this turn and HALT regardless of ``hard_stop_enabled`` — mirroring the
    # always-on per-URL cap in ``tools/browser_navigate_fallback`` — so a browser
    # retry spiral is bounded even in the default (hard-stop-off) mode. ``0``
    # disables the browser cap (falls back to the generic same-tool behaviour).
    browser_failure_cap: int = 3
    # #974/#969/#970 — terminal and execute_code are the system's largest
    # failure sources (1237 terminal failures / 410 sessions, 59 execute_code
    # failures / 14 sessions, 26 read_file failures / 10 sessions). Four prior
    # fixes (#942, #863, #888, #902) closed completed but the problem worsened
    # because the loop_guard's fallback_directive is advisory — the agent
    # ignores it and retries. This cap is an always-on enforcement gate
    # (independent of ``hard_stop_enabled``) that halts the turn after N
    # consecutive same-tool failures, mirroring the browser_failure_cap pattern.
    # The fallback_directive is surfaced on the halt decision so the agent sees
    # a concrete alternative action. ``0`` disables the cap.
    spiral_failure_cap: int = 5
    # #1825 — per-tool override of the spiral failure cap. Memory tools have
    # a high false-retry rate (161 failures/7d, 11-deep spirals) and should
    # be capped at a lower threshold (3) so the session-hard-stop fires sooner.
    # Keys are tool names; values override spiral_failure_cap for that tool.
    per_tool_failure_caps: dict[str, int] = field(
        default_factory=lambda: {
            "memory": 3,
            "read_file": 10,
            "search_files": 10,
        }
    )
    spiral_prone_tools: frozenset[str] = field(
        default_factory=lambda: _SPIRAL_PRONE_TOOLS
    )
    idempotent_tools: frozenset[str] = field(
        default_factory=lambda: IDEMPOTENT_TOOL_NAMES
    )
    mutating_tools: frozenset[str] = field(default_factory=lambda: MUTATING_TOOL_NAMES)
    loop_caps: "LoopCapConfig" = field(default_factory=lambda: LoopCapConfig())

    @classmethod
    def from_mapping(
        cls, data: Mapping[str, Any] | None, *, platform: str | None = None,
    ) -> ToolCallGuardrailConfig:
        """Build config from `tool_loop_guardrails`; nested ``warn_after`` / ``hard_stop_after`` win over flat legacy keys."""
        if not isinstance(data, Mapping):
            data = {}

        warn_after = data.get("warn_after")
        if not isinstance(warn_after, Mapping):
            warn_after = {}
        hard_stop_after = data.get("hard_stop_after")
        if not isinstance(hard_stop_after, Mapping):
            hard_stop_after = {}

        defaults = cls()
        hard_stop_enabled = _as_bool(data.get("hard_stop_enabled"), defaults.hard_stop_enabled)
        non_interactive_hard_stop_enabled = _as_bool(
            data.get("non_interactive_hard_stop_enabled"),
            defaults.non_interactive_hard_stop_enabled,
        )
        if _is_non_interactive_platform(platform) and non_interactive_hard_stop_enabled:
            hard_stop_enabled = True

        return cls(
            warnings_enabled=_as_bool(data.get("warnings_enabled"), defaults.warnings_enabled),
            hard_stop_enabled=hard_stop_enabled,
            non_interactive_hard_stop_enabled=non_interactive_hard_stop_enabled,
            exact_failure_warn_after=_positive_int(
                warn_after.get("exact_failure", data.get("exact_failure_warn_after")),
                defaults.exact_failure_warn_after,
            ),
            same_tool_failure_warn_after=_positive_int(
                warn_after.get(
                    "same_tool_failure", data.get("same_tool_failure_warn_after")
                ),
                defaults.same_tool_failure_warn_after,
            ),
            no_progress_warn_after=_positive_int(
                warn_after.get(
                    "idempotent_no_progress", data.get("no_progress_warn_after")
                ),
                defaults.no_progress_warn_after,
            ),
            exact_failure_block_after=_positive_int(
                hard_stop_after.get(
                    "exact_failure", data.get("exact_failure_block_after")
                ),
                defaults.exact_failure_block_after,
            ),
            same_tool_failure_halt_after=_positive_int(
                hard_stop_after.get(
                    "same_tool_failure", data.get("same_tool_failure_halt_after")
                ),
                defaults.same_tool_failure_halt_after,
            ),
            no_progress_block_after=_positive_int(
                hard_stop_after.get(
                    "idempotent_no_progress", data.get("no_progress_block_after")
                ),
                defaults.no_progress_block_after,
            ),
            browser_failure_cap=_non_negative_int(
                data.get("browser_failure_cap"),
                defaults.browser_failure_cap,
            ),
            spiral_failure_cap=_non_negative_int(
                data.get("spiral_failure_cap"),
                defaults.spiral_failure_cap,
            ),
            per_tool_failure_caps=_merge_per_tool_caps(
                data.get("per_tool_failure_caps"), defaults.per_tool_failure_caps
            ),
            loop_caps=LoopCapConfig.from_mapping(data.get("loop_caps")),
        )


@dataclass(frozen=True)
class IdenticalCallObservation:
    """``notice`` is appended after the result, ``stub`` replaces a byte-identical duplicate result;
    ``kind`` names the detector behind the notice (``identical_call_streak`` / ``identical_cycle``)."""

    notice: str | None = None
    stub: str | None = None
    kind: str | None = None


@dataclass(frozen=True)
class ToolCallSignature:
    """Stable, non-reversible identity for a tool name plus canonical args."""

    tool_name: str
    args_hash: str

    @classmethod
    def from_call(cls, tool_name: str, args: Mapping[str, Any] | None) -> ToolCallSignature:
        return cls(tool_name=tool_name, args_hash=_sha256(canonical_tool_args(args or {})))

    def to_metadata(self) -> dict[str, str]:
        """Return public metadata without raw argument values."""
        return {"tool_name": self.tool_name, "args_hash": self.args_hash}


@dataclass(frozen=True)
class ToolGuardrailDecision:
    """Decision returned by the tool-call guardrail controller."""

    action: str = "allow"  # allow | warn | block | halt
    code: str = "allow"
    message: str = ""
    tool_name: str = ""
    count: int = 0
    signature: ToolCallSignature | None = None
    # #744/#785 — structured fallback guidance for non-retryable failures.
    # Populated on warn/halt decisions arising from repeated tool failures so
    # the agent loop (or a policy interceptor) can surface a concrete
    # alternative action instead of only a free-text message.
    fallback_directive: str = ""

    @property
    def allows_execution(self) -> bool:
        return self.action in {"allow", "warn"}

    @property
    def should_halt(self) -> bool:
        return self.action in {"block", "halt"}

    def to_metadata(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "action": self.action,
            "code": self.code,
            "message": self.message,
            "tool_name": self.tool_name,
            "count": self.count,
        }
        if self.signature is not None:
            data["signature"] = self.signature.to_metadata()
        if self.fallback_directive:
            data["fallback_directive"] = self.fallback_directive
        return data


def canonical_tool_args(args: Mapping[str, Any]) -> str:
    """Return sorted compact JSON for parsed tool arguments."""
    if not isinstance(args, Mapping):
        raise TypeError(f"tool args must be a mapping, got {type(args).__name__}")
    return json.dumps(
        args,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def classify_tool_failure(tool_name: str, result: str | None) -> tuple[bool, str]:
    """Safety-fallback classifier used only when callers don't pass ``failed``.

    Mirrors ``agent.display._detect_tool_failure`` exactly so the guardrail
    never disagrees with the CLI's user-visible ``[error]`` tag. Production
    callers in ``run_agent.py`` always pass an explicit ``failed=`` derived
    from ``_detect_tool_failure``; this function exists so standalone callers
    (tests, tooling) still get consistent behavior.
    """
    if result is None:
        return False, ""
    if file_mutation_result_landed(tool_name, result):
        return False, ""

    # A harness REFUSAL of a redundant call (repeated identical read/search) carries
    # ``"error"`` for the model's benefit -- exactly what the substring test below keys
    # on -- but nothing failed; counting it lets the cheap refusal feed the streak that
    # fires the next, harder one. Mirrored in ``agent.display._detect_tool_failure``.
    if is_guardrail_refusal(result):
        return False, ""

    if tool_name == "terminal":
        data = safe_json_loads(result)
        exit_code = data.get("exit_code") if isinstance(data, dict) else None
        return (True, f" [exit {exit_code}]") if exit_code is not None and exit_code != 0 else (False, "")

    data = safe_json_loads(result)
    if isinstance(data, dict):
        failed = data.get("success") is False
        if tool_name == "memory" and failed and "exceed the limit" in data.get("error", ""):
            return True, " [full]"
        # Same rule as _detect_tool_failure: a JSON object is judged by its top-level
        # shape, never by a substring scan of its nested content.
        err = data.get("error") or data.get("message")
        return (True, " [error]") if (err and (failed or "error" in data)) or shape_failure_error(data) else (False, "")
    return (True, " [error]") if has_failure_marker(result) else (False, "")


# Guardrail verdict text injected into the conversation, keyed by decision code.
# ``same_tool_failure_warning`` is built by _tool_failure_recovery_hint (tool-specific).
_DECISION_MESSAGES: dict[str, str] = {
    "repeated_exact_failure_block": (
        "Blocked {tool_name}: the same tool call failed {count} times with identical arguments. "
        "Stop retrying it unchanged; change strategy or explain the blocker."
    ),
    "idempotent_no_progress_block": (
        "Blocked {tool_name}: this read-only call returned the same result {count} times. "
        "Stop repeating it unchanged; use the result already provided or try a different query."
    ),
    "same_tool_failure_halt": (
        "Stopped {tool_name}: it failed {count} times this turn. "
        "Stop retrying the same failing tool path and choose a different approach."
    ),
    "repeated_exact_failure_warning": (
        "{tool_name} has failed {count} times with identical arguments. This looks like a loop; "
        "inspect the error and change strategy instead of retrying it unchanged."
    ),
    "idempotent_no_progress_warning": (
        "{tool_name} returned the same result {count} times. Use the result already provided "
        "or change the query instead of repeating it unchanged."
    ),
    "identical_call_streak_halt": (
        "Stopped {tool_name}: the same call with identical arguments returned the same result "
        "{count} times in a row. Stop repeating it unchanged; use the result already provided or change strategy."
    ),
    "identical_cycle_halt": (
        "Stopped {tool_name}: the same repeating cycle of tool calls (period {period}) with identical "
        "arguments and identical results has run {count} times. Repeating the batch unchanged is not "
        "progress; use the results already provided or change strategy."
    ),
    "loop_web_search_cap": (
        "Blocked web_search: this turn has already made {cap} web searches, the per-turn limit. "
        "This looks like a runaway search loop. Work with the results you already have and give the user your answer."
    ),
    "loop_subagent_cap": (
        "Blocked delegate_task: this turn has already spawned {count} subagents (limit {cap}). "
        "This looks like a runaway delegation loop. Finish the work with the results you have and answer the user."
    ),
}

_IDENTICAL_CALL_NOTICE = (
    "[hermes note: this is the {ordinal} consecutive identical call to "
    "{tool_name} with identical arguments returning the same result. "
    "Do not repeat it — change arguments, use a different tool, or "
    "proceed with what you have.]"
)

_IDENTICAL_CYCLE_NOTICE = (
    "[hermes note: the last {count} rounds repeated the same cycle of {period} tool calls "
    "(ending with {tool_name}) with identical arguments and identical results. "
    "Do not repeat the batch — change arguments, use a different tool, or "
    "proceed with what you have.]"
)

# tool -> (LoopCapConfig field, controller counter attribute, decision code)
_LOOP_CAPS: dict[str, tuple[str, str, str]] = {
    "web_search": ("max_web_searches", "_turn_web_search_count", "loop_web_search_cap"),
    "delegate_task": ("max_subagents", "_turn_subagent_count", "loop_subagent_cap"),
}


class ToolCallGuardrailController:
    """Per-turn controller for repeated failed/non-progressing tool calls.

    Optionally evaluates a pluggable :class:`PolicyInterceptorRegistry` (passed
    as ``policy_registry``) *before* the loop/limit checks. Policy denials are
    hard constraints independent of ``hard_stop_enabled`` — that flag only
    governs the loop-limit circuit breaker, not user-authored policies.
    """

    def __init__(
        self,
        config: ToolCallGuardrailConfig | None = None,
        policy_registry: "PolicyInterceptorRegistry | None" = None,
        recheck_controller: "RecheckController | None" = None,
    ):
        self.config = config or ToolCallGuardrailConfig()
        self.policy_registry = policy_registry
        # #1041 — optional recheck-suppression controller. When present and
        # enabled it can suppress a single redundant read-only recheck in
        # ``before_call``; None (the default) is a full no-op.
        self.recheck_controller = recheck_controller
        # Cross-turn failure streaks — NOT reset by reset_for_turn so that
        # one-failing-call-per-turn spirals (the common pattern: the model
        # calls the same failing tool once per API turn) accumulate across
        # turns and trigger the cap.  reset_for_turn only clears per-turn
        # bookkeeping (exact-failure, no-progress, halt_decision).
        self._cross_turn_tool_failure_counts: dict[str, int] = {}
        # #1585 — track consecutive successes per spiral-prone tool so we
        # only drain the failure streak after a SUSTAINED recovery (multiple
        # successes in a row), not on a single interspersed success. Without
        # this, the fail→diagnostic-success→fail pattern (the production
        # spiral) nets 0 per cycle and the cap is unreachable.
        self._cross_turn_success_streaks: dict[str, int] = {}
        # #1826 — session-level permanent hard-stop set. Once a spiral-prone
        # tool's cross-turn streak reaches the cap, the tool is added here and
        # ALL subsequent calls are permanently blocked for the session. This is
        # the unconditional ceiling that does NOT depend on error classification
        # and CANNOT be decayed by interspersed successes. reset_for_turn does
        # NOT clear this — it survives the entire session by design.
        self._session_hard_stopped: set[str] = set()
        self.reset_for_turn()

    def reset_for_turn(self) -> None:
        self._exact_failure_counts: dict[ToolCallSignature, int] = {}
        self._same_tool_failure_counts: dict[str, int] = {}
        # signature -> a mutating call succeeded since its last failure
        self._progress_since_failure: dict[ToolCallSignature, bool] = {}
        self._no_progress: dict[ToolCallSignature, tuple[str, int]] = {}
        self._halt_decision: ToolGuardrailDecision | None = None
        # #1041 — last executed call, for immediate-recheck detection.
        self._last_signature: ToolCallSignature | None = None
        self._last_call_succeeded: bool = False
        if self.policy_registry is not None:
            self.policy_registry.reset_for_turn()
        # Identical-call loop-breaker state (agent.stall_guards): tracks the
        # CONSECUTIVE streak of identical (tool, canonical args) calls whose
        # results were also identical. Any different call — or a different
        # result — resets the streak, so legitimate re-reads after edits and
        # varied polling are never flagged. Per-turn, like everything else here.
        # NOTE: open PR #85352 (patrykkopycinski) tracks no-progress loops
        # ACROSS turns via a detection window — a different mechanism from
        # this per-turn consecutive streak. Coordinate future work there.
        self._identical_streak_sig: ToolCallSignature | None = None
        self._identical_streak_result_hash: str = ""
        self._identical_streak_count: int = 0
        # tool_call_id of the FIRST call in the current streak, so a
        # result-reference stub can point at the message that carries the
        # full payload.
        self._identical_streak_first_call_id: str = ""
        # Batch-cycle loop breaker (port of can1357/oh-my-pi#10521): sequence of
        # (signature, result_hash, repeatable) for every observed call this turn, so a repeating
        # multi-call cycle (A,B,A,B,...) is caught even though it resets the consecutive streak above.
        self._call_history: deque[tuple[ToolCallSignature, str, bool]] = deque(maxlen=_STALL_GUARD_CYCLE_HISTORY)
        # tool_call_id -> spillover path, so a stub referencing a persisted-output preview can't dangle.
        self._persisted_result_paths: dict[str, str] = {}
        # Per-turn runaway-loop cap counters. Reset every turn (this method
        # runs at the start of each run_conversation), so the caps bound a
        # single agent loop rather than accumulating across the session.
        self._turn_web_search_count = 0
        self._turn_subagent_count = 0

    @property
    def halt_decision(self) -> ToolGuardrailDecision | None:
        return self._halt_decision

    def _decide(
        self, action: str, code: str, tool_name: str, count: int, signature: ToolCallSignature,
        *, message: str | None = None, **fmt: Any,
    ) -> ToolGuardrailDecision:
        """Build a warn/block/halt decision; block/halt is also recorded as the turn's halt decision."""
        if message is None:
            message = _DECISION_MESSAGES[code].format(tool_name=tool_name, count=count, **fmt)
        decision = ToolGuardrailDecision(action, code, message, tool_name, count, signature)
        if decision.should_halt:
            self._halt_decision = decision
        return decision

    def before_call(self, tool_name: str, args: Mapping[str, Any] | None) -> ToolGuardrailDecision:
        args = _coerce_args(args)
        signature = ToolCallSignature.from_call(tool_name, args)
        allow = ToolGuardrailDecision(tool_name=tool_name, signature=signature)

        # Loop caps apply regardless of hard_stop_enabled (which only governs the detector).
        cap_block = self._check_loop_cap(tool_name, args, signature)
        if cap_block is not None or not self.config.hard_stop_enabled:
            return cap_block or allow
        # A mutation since this call last failed makes the retry a new experiment.
        exact_count = 0 if self._progress_since_failure.get(signature) else self._exact_failure_counts.get(signature, 0)
        if exact_count >= self.config.exact_failure_block_after:
            return self._decide("block", "repeated_exact_failure_block", tool_name, exact_count, signature)
        record = self._no_progress.get(signature) if self._is_idempotent(tool_name) else None
        if record is not None and record[1] >= self.config.no_progress_block_after:
            return self._decide("block", "idempotent_no_progress_block", tool_name, record[1], signature)
        return allow

    def after_call(
        self, tool_name: str, args: Mapping[str, Any] | None, result: str | None,
        *, failed: bool | None = None,
    ) -> ToolGuardrailDecision:
        args = _coerce_args(args)
        signature = ToolCallSignature.from_call(tool_name, args)
        if failed is None:
            failed, _ = classify_tool_failure(tool_name, result)
        warnings = self.config.warnings_enabled

        if failed:
            # An identical failing call is only a REPLAY if nothing landed in between;
            # a mutation since the last identical failure restarts the exact-args streak.
            if self._progress_since_failure.pop(signature, False):
                self._exact_failure_counts.pop(signature, None)
            # A throttled call retried after its cooldown is the correct move, not a replay:
            # keep it out of the exact-args streak and never give it that streak's "stop retrying
            # unchanged" warning, even after earlier ordinary failures with the same args.
            # It still counts per tool below, so same_tool_failure_halt bounds a dead endpoint.
            throttled = is_rate_limited(result)
            exact_count = self._exact_failure_counts.get(signature, 0)
            if not throttled:
                exact_count = self._exact_failure_counts[signature] = exact_count + 1
            same_count = self._same_tool_failure_counts[tool_name] = self._same_tool_failure_counts.get(tool_name, 0) + 1
            self._no_progress.pop(signature, None)
            # same_tool_failure counts DIFFERENT args on one tool; for failure-tolerant
            # tools a run of distinct red commands is diagnosis, not a loop — warn, never halt.
            if (
                # Hard-stop widening (#89069 / #100849 bundle): the per-turn no-progress BLOCK above only
                # covers tools in idempotent_tools, so a model replaying the same successful
                # `terminal`/`skill_view` call with a byte-identical result ran until the iteration budget.
                # The consecutive-identical streak is tool-agnostic; when hard stops are enabled, halt at
                # the same idempotent_no_progress threshold. Pollers stay exempt (an unchanged poll is
                # progress).
                self.config.hard_stop_enabled
                and tool_name not in FAILURE_TOLERANT_TOOL_NAMES
                and same_count >= self.config.same_tool_failure_halt_after
            ):
                return self._decide("halt", "same_tool_failure_halt", tool_name, same_count, signature)
            if warnings and not throttled and exact_count >= self.config.exact_failure_warn_after:
                return self._decide("warn", "repeated_exact_failure_warning", tool_name, exact_count, signature)
            if warnings and same_count >= self.config.same_tool_failure_warn_after:
                return self._decide(
                    "warn", "same_tool_failure_warning", tool_name, same_count, signature,
                    message=_tool_failure_recovery_hint(tool_name, same_count),
                )
            return ToolGuardrailDecision(tool_name=tool_name, count=exact_count, signature=signature)

        self._exact_failure_counts.pop(signature, None)
        self._same_tool_failure_counts.pop(tool_name, None)
        # A successful mutation is progress for every failing signature still counted
        # this turn. Pure loops never mutate between attempts, so the replay detector keeps its teeth.
        if tool_name in PROGRESS_RESET_TOOL_NAMES or file_mutation_result_landed(tool_name, result):
            self._progress_since_failure.update(dict.fromkeys(self._exact_failure_counts, True))
            self._same_tool_failure_counts.clear()
        if not self._is_idempotent(tool_name):
            self._no_progress.pop(signature, None)
            return ToolGuardrailDecision(tool_name=tool_name, signature=signature)

        result_hash = _result_hash(result, tool_name)
        previous = self._no_progress.get(signature)
        repeat_count = previous[1] + 1 if previous is not None and previous[0] == result_hash else 1
        self._no_progress[signature] = (result_hash, repeat_count)
        if warnings and repeat_count >= self.config.no_progress_warn_after:
            return self._decide("warn", "idempotent_no_progress_warning", tool_name, repeat_count, signature)
        return ToolGuardrailDecision(tool_name=tool_name, count=repeat_count, signature=signature)

    def _is_idempotent(self, tool_name: str) -> bool:
        return tool_name not in self.config.mutating_tools and tool_name in self.config.idempotent_tools

    def observe_call(
        self, tool_name: str, args: Mapping[str, Any] | None, result: str | None,
        *, tool_call_id: str = "", failed: bool = False,
    ) -> IdenticalCallObservation:
        """Track consecutive identical calls; return notice + dedupe stub info.

        ``notice`` fires from the threshold-th consecutive identical (tool, args, result) call
        (observational, pollers exempt). ``stub`` replaces the CURRENT result from the 2nd byte-identical
        repeat — the tool still executed, only the context representation is deduplicated, so polling
        semantics survive; pollers are NOT exempt here since an unchanged poll is where it saves most.
        """
        is_plain_str = isinstance(result, str)
        signature = ToolCallSignature.from_call(tool_name, _coerce_args(args))
        result_hash = _result_hash(result, tool_name) if is_plain_str else ""

        if is_plain_str and (signature, result_hash) == (self._identical_streak_sig, self._identical_streak_result_hash):
            self._identical_streak_count += 1
        else:
            # New streak; non-string (multimodal) results never form one.
            self._identical_streak_sig = signature if is_plain_str else None
            self._identical_streak_result_hash = result_hash
            self._identical_streak_count = 1 if is_plain_str else 0
            self._identical_streak_first_call_id = tool_call_id or ""
        count = self._identical_streak_count

        notice = kind = None
        if not is_stall_guard_repeatable(tool_name) and count >= STALL_GUARD_IDENTICAL_CALL_THRESHOLD:
            notice, kind = _IDENTICAL_CALL_NOTICE.format(ordinal=_ordinal(count), tool_name=tool_name), "identical_call_streak"
            # The no-progress BLOCK in before_call only covers idempotent_tools; this streak
            # is tool-agnostic, so with hard stops on, halt at the same threshold (a model
            # replaying a successful `terminal` call otherwise runs to the budget).
            if self.config.hard_stop_enabled and count >= self.config.no_progress_block_after and self._halt_decision is None:
                self._decide("halt", "identical_call_streak_halt", tool_name, count, signature)

        # Batch-cycle detection (oh-my-pi#10521): a repeating multi-call cycle resets the
        # consecutive streak on every alternation, so check the call history for a period-p lap.
        if is_plain_str:
            self._call_history.append((signature, result_hash, is_stall_guard_repeatable(tool_name)))
        else:
            self._call_history.clear()
        if notice is None and is_plain_str:
            cycle = self._detect_identical_cycle()
            if cycle is not None:
                period, laps = cycle
                notice, kind = _IDENTICAL_CYCLE_NOTICE.format(count=laps, period=period, tool_name=tool_name), "identical_cycle"
                if self.config.hard_stop_enabled and laps >= self.config.no_progress_block_after and self._halt_decision is None:
                    self._decide("halt", "identical_cycle_halt", tool_name, laps, signature, period=period)

        stub = None
        if (
            is_plain_str
            and count >= 2
            and not failed
            and len(result) >= IDENTICAL_RESULT_STUB_MIN_CHARS
        ):
            stub = self._build_result_reference_stub(tool_name, args)
        return IdenticalCallObservation(notice=notice, stub=stub, kind=kind)

    def _detect_identical_cycle(self) -> tuple[int, int] | None:
        """Detect a repeating identical-call cycle ending at the latest observed call.

        Returns ``(period, laps)`` for the smallest period 2..max whose trailing laps
        (identical signature AND result per position) reach the notice threshold, else None.
        Period 1 is the consecutive streak's job. A cycle made ONLY of poller-exempt tools
        is exempt (an unchanged poll loop is legitimate waiting); one non-exempt call in
        the cycle keeps the guard armed, matching the single-call exemption semantics.
        """
        history = self._call_history
        for period in range(2, _STALL_GUARD_MAX_CYCLE_PERIOD + 1):
            if len(history) < period * STALL_GUARD_IDENTICAL_CALL_THRESHOLD:
                continue
            laps = 1
            # Count how many consecutive trailing laps equal the final lap.
            while True:
                base = len(history) - period * (laps + 1)
                if base < 0:
                    break
                lap_equal = all(
                    history[base + i][:2] == history[len(history) - period + i][:2]
                    for i in range(period)
                )
                if not lap_equal:
                    break
                laps += 1
            if laps >= STALL_GUARD_IDENTICAL_CALL_THRESHOLD:
                tail = [history[len(history) - period + i] for i in range(period)]
                if all(repeatable for _, _, repeatable in tail):
                    continue
                # A constant sub-cycle would already have fired at a smaller period.
                return period, laps
        return None

    def record_persisted_result(self, tool_call_id: str, file_path: str) -> None:
        """Remember the spillover path a persisted result was saved to.

        When the first occurrence of a result entered context as a
        persisted-output preview, a later reference stub must carry the
        spillover file path so the reference can't dangle.
        """
        if tool_call_id and file_path:
            self._persisted_result_paths[tool_call_id] = file_path

    def _build_result_reference_stub(
        self, tool_name: str, args: Mapping[str, Any] | None
    ) -> str:
        """Build the reference stub replacing a byte-identical duplicate result.

        Carries the tool name + a canonical-args preview so that even if
        context compression later evicts the referenced result, the model
        still knows WHAT the call was (cheap dangling-reference mitigation).
        """
        try:
            args_preview = canonical_tool_args(_coerce_args(args))
        except TypeError:
            args_preview = "{}"
        if len(args_preview) > _RESULT_STUB_ARGS_PREVIEW_CHARS:
            args_preview = args_preview[:_RESULT_STUB_ARGS_PREVIEW_CHARS] + "…"
        first_id = self._identical_streak_first_call_id
        ref = f" (tool_call_id {first_id})" if first_id else ""
        stub = (
            f"[hermes note: this result is byte-identical to the {tool_name} "
            f"result earlier this turn{ref}. Refer to that result; it has not "
            f"changed. Args: {args_preview}]"
        )
        spill_path = self._persisted_result_paths.get(first_id) if first_id else None
        if spill_path:
            stub += (
                f"\n[The referenced result was persisted to: {spill_path} — "
                "page through it with read_file if you need the full content.]"
            )
        return stub

    def _check_loop_cap(
        self,
        tool_name: str,
        args: Mapping[str, Any],
        signature: ToolCallSignature,
    ) -> ToolGuardrailDecision | None:
        """Enforce and advance the per-turn runaway-loop counters.

        Returns a ``block`` decision when the cap is already reached, otherwise
        increments the relevant counter for the allowed call and returns
        ``None``. A cap of 0 disables that limit entirely. Counters reset each
        turn via ``reset_for_turn``.
        """
        caps = self.config.loop_caps

        if tool_name == "web_search":
            cap = caps.max_web_searches
            if cap and self._turn_web_search_count >= cap:
                decision = ToolGuardrailDecision(
                    action="block",
                    code="loop_web_search_cap",
                    message=(
                        f"Blocked web_search: this turn has already made {cap} "
                        "web searches, the per-turn limit. This looks like a "
                        "runaway search loop. Work with the results you already "
                        "have and give the user your answer."
                    ),
                    tool_name=tool_name,
                    count=self._turn_web_search_count,
                    signature=signature,
                )
                self._halt_decision = decision
                return decision
            self._turn_web_search_count += 1
            return None

        if tool_name == "delegate_task":
            cap = caps.max_subagents
            if not cap:
                return None
            spawn_count = _subagent_spawn_count(args)
            if spawn_count == 0:
                # Control action (list/steer/stop) — spawns nothing. Never
                # block: once the spawn cap is hit, steering/stopping the
                # existing children is exactly what should still work.
                return None
            if self._turn_subagent_count >= cap:
                decision = ToolGuardrailDecision(
                    action="block",
                    code="loop_subagent_cap",
                    message=(
                        f"Blocked delegate_task: this turn has already spawned "
                        f"{self._turn_subagent_count} subagents (limit {cap}). "
                        "This looks like a runaway delegation loop. Finish the "
                        "work with the results you have and answer the user."
                    ),
                    tool_name=tool_name,
                    count=self._turn_subagent_count,
                    signature=signature,
                )
                self._halt_decision = decision
                return decision
            self._turn_subagent_count += spawn_count
            return None

        return None


def toolguard_synthetic_result(decision: ToolGuardrailDecision) -> str:
    """Build a synthetic role=tool content string for a blocked tool call.

    When the decision carries a ``fallback_directive`` (#744/#785/#787), it is
    surfaced as a top-level field so the model sees a concrete alternative
    action instead of only the free-text error message.
    """
    payload: dict[str, Any] = {
        "error": decision.message,
        "guardrail": decision.to_metadata(),
    }
    if decision.fallback_directive:
        payload["fallback_directive"] = decision.fallback_directive
    return json.dumps(payload, ensure_ascii=False)


def append_toolguard_guidance(result: str, decision: ToolGuardrailDecision) -> str:
    """Append runtime guidance to the current tool result content.

    When the decision carries a ``fallback_directive`` (#744/#785/#787), the
    directive is appended as a separate labelled line so the model sees a
    concrete alternative action alongside the loop warning.
    """
    if decision.action not in {"warn", "halt"} or not decision.message:
        return result
    label = "Tool loop hard stop" if decision.action == "halt" else "Tool loop warning"
    suffix = (
        f"\n\n[{label}: {decision.code}; count={decision.count}; {decision.message}]"
    )
    if decision.fallback_directive:
        suffix += f"\n[Fallback directive: {decision.fallback_directive}]"
    return (result or "") + suffix


def append_non_retryable_notice(result: str, notice: str) -> str:
    """Attach a non-retryable diagnostic without splitting a JSON tool result.

    A trailing suffix after one JSON object is a second value: ``json.loads``
    of the whole tool message raises Extra data, so previews and session rows
    cannot read the refusal. Fold the notice into that object's ``error``
    string. Plain-text results keep the trailing suffix.
    """
    if not isinstance(result, str) or not notice or "Non-retryable:" in result:
        return result
    text = result
    stripped = text.strip()
    payload = safe_json_loads(stripped) if stripped.startswith("{") else None
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, str):
            payload["error"] = f"{error}{notice}"
        elif not error:
            payload["error"] = notice.strip()
        else:
            payload["non_retryable"] = notice.strip()
        return json.dumps(payload, ensure_ascii=False)
    return text + notice


def _tool_failure_recovery_hint(tool_name: str, count: int) -> str:
    """Action-oriented guidance for recovering from repeated tool failures."""
    common = (
        f"{tool_name} has failed {count} times this turn. This looks like a loop. "
        "Do not switch to text-only replies; keep using tools, but diagnose before retrying. "
        "First inspect the latest error/output and verify your assumptions. "
    )
    if tool_name == "terminal":
        return common + (
            "For terminal failures, run a small diagnostic such as `pwd && ls -la` "
            "in the same tool, then try an absolute path, a simpler command, a different "
            "working directory, or a different tool such as read_file/write_file/patch."
        )
    return common + (
        "Try different arguments, a narrower query/path, an absolute path when relevant, "
        "or a different tool that can make progress. If the blocker is external, report "
        "the blocker after one diagnostic attempt instead of repeating the same failing path."
    )


# #744/#785 — concise, structured fallback directives keyed by tool name.
# Unlike _tool_failure_recovery_hint (which is a free-text nudge), these are
# short imperative phrases suitable for structured consumption by the agent
# loop or policy interceptors: "use <alternative> instead".
_TOOL_FALLBACK_DIRECTIVE: dict[str, str] = {
    "read_file": "use search_files to locate the file, or vision_analyze for binary/image files",
    "terminal": "run a read-only diagnostic (pwd, ls) before retrying, or switch to read_file/patch; for timeouts, use background=true with notify_on_complete=true instead of retrying in foreground",
    "execute_code": "install missing packages via terminal, or verify the interpreter/venv first",
    "web_search": "try web_extract on a known URL, or refine the query terms",
    "web_extract": "try web_search to find alternative URLs, or use the browser tool",
    "search_files": "no results found repeatedly — switch strategy: (a) use target=files mode instead of content, (b) broaden the directory path, (c) try a different glob pattern instead of regex, or (d) ask the user for the correct path",
    "patch": "use read_file to verify the exact text before patching, or use write_file",
    "write_file": "verify the directory exists with terminal, or use patch for targeted edits; for parse-errors, fix the syntax in the content argument — the same malformed content will fail identically on retry",
    "process": "use process action=list to find the correct session_id before retrying",
    "process_manage": "use process_manage action=list to find the correct session_id before retrying",
    "todo": "use todo action=list to check current items before retrying",
    "todo_list": "use todo_list action=list to check current items before retrying",
    "cronjob": "use cronjob action=list to inspect scheduled jobs before retrying",
    "cronjob_manage": "use cronjob_manage action=list to inspect scheduled jobs before retrying",
    "tour": "use tour action=list to inspect available tour steps",
    "gui_tour": "use gui_tour action=list to inspect available tour steps",
    "tip": "verify tip identifier or category before retrying",
    "show_tip": "verify tip identifier or category before retrying",
    # #739 — media tools: a failed visual call is usually a bad path/format or an
    # unavailable provider, not something a blind retry fixes. Route to a check
    # or a text fallback instead of spiraling on the same call.
    "vision_analyze": "verify the image path exists and is a supported format (png/jpg/webp) with read_file, or proceed from a text description instead of retrying",
    "image_generate": "report the visual blocker and supply a text description/placeholder instead of retrying, or verify the prompt and image-provider configuration",
    "video_analyze": "verify the video path and format with read_file, or work from a text summary of the video instead of retrying",
    "video_generate": "report the visual blocker and supply a text placeholder instead of retrying, or verify the prompt and video-provider configuration",
    # #745 — browser tools: a deterministic browser failure (backend down, nav
    # timeout, stale ref) does not recover on a blind retry. Route to the
    # web_extract/web_search text fallback or a fresh snapshot instead of
    # re-driving the browser.
    "browser_navigate": "use web_extract or web_search for this URL instead of re-navigating; the page-text fallback is in the last result",
    "browser_click": "re-run browser_snapshot to refresh element refs, or extract the page text with web_extract instead of retrying the same ref",
    "browser_type": "re-run browser_snapshot to refresh element refs, or extract the page text with web_extract instead of retrying the same ref",
    "browser_console": "the JS eval failed deterministically; read values via browser_snapshot or extract the page with web_extract instead of re-evaluating",
    # #1185 — tool_call (deferred-tool invocation) failures spiral because the
    # agent retries the same failing deferred call (wrong args schema, tool
    # unavailable, provider rejection) instead of switching strategy. Route
    # to a search/describe refresh or a core tool rather than blind-retrying.
    "tool_call": "do not retry the same deferred tool with the same args — run tool_search for an alternative, use tool_describe to validate the schema first, or invoke a core tool (terminal/read_file/search_files) instead",
    # #1187 — tool_describe is the middle step of the search→describe→call
    # chain; when it fails the agent has no schema to invoke the deferred tool.
    # Re-running tool_search refreshes the catalog; retrying the same name is
    # deterministic and won't recover.
    "tool_describe": "the schema lookup is not resolving — re-run tool_search to refresh the catalog, verify the exact tool name/spelling from the search results, and do not keep describing the same failing name",
    # #1186 — memory failures (94/21 sessions, 11-deep) regressed from
    # #1135/#1136. The store may be busy/locked (transient) or genuinely
    # failed; blind retries don't recover. Distinguish transient from hard
    # failure, retry once if busy, else skip-and-continue.
    "memory": "memory operation failed repeatedly — distinguish busy/locked (transient, retry once) from genuine failure (non-retryable); if not transient, memory tools are unavailable this session — proceed without them and log the unmet persistence need",
}

# #745 — generic fallback for any browser_* tool not explicitly listed above.
_BROWSER_FALLBACK_DIRECTIVE = (
    "stop re-driving the browser; use web_extract/web_search on the target URL, "
    "or work from the page text already retrieved, instead of retrying"
)


def _is_browser_tool(tool_name: str) -> bool:
    """Whether ``tool_name`` is a browser tool subject to the browser retry cap."""
    return tool_name.startswith("browser_")


def _fallback_directive_for(tool_name: str) -> str:
    """Return a concise fallback directive for a failing tool, or empty string."""
    directive = _TOOL_FALLBACK_DIRECTIVE.get(tool_name)
    if directive is not None:
        return directive
    if _is_browser_tool(tool_name):
        return _BROWSER_FALLBACK_DIRECTIVE
    return ""


def _coerce_args(args: Mapping[str, Any] | None) -> Mapping[str, Any]:
    return args if isinstance(args, Mapping) else {}


def _merge_per_tool_caps(raw: Any, defaults: dict[str, int]) -> dict[str, int]:
    """Parse the ``per_tool_failure_caps`` config section (#1825).

    Accepts a mapping of ``{tool_name: cap}`` from config.yaml and merges it
    over the defaults. Invalid entries (non-int, negative) are silently
    dropped so a typo in config doesn't crash the agent loop.
    """
    if not isinstance(raw, Mapping):
        return dict(defaults)
    merged = dict(defaults)
    for key, value in raw.items():
        if not isinstance(key, str):
            continue
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            continue
        if parsed >= 1:
            merged[key] = parsed
    return merged


# execute_code reports per-call kernel bookkeeping — a running `kernel.execution_count` and wall-clock
# `duration_seconds` — that changes on every invocation even when the code did the same thing. Hashed
# as-is, every replay of an identical empty probe looks new and the identical-call streak never forms
# (a model re-ran one empty execute_code call 147 times unflagged). Only these known locations in
# execute_code's own result shape are dropped: for any other tool the same key names can be real output.
def _without_execute_code_metadata(parsed: Any) -> Any:
    if not isinstance(parsed, dict):
        return parsed
    cleaned = {k: v for k, v in parsed.items() if k != "duration_seconds"}
    kernel = cleaned.get("kernel")
    if isinstance(kernel, dict):
        cleaned["kernel"] = {k: v for k, v in kernel.items() if k != "execution_count"}
    return cleaned


def _canonical_json(value: Any) -> str:
    """Return sorted compact JSON for parsed tool output."""
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def _ordinal(count: int) -> str:
    return f"{count}{'th' if 11 <= count % 100 <= 13 else {1: 'st', 2: 'nd', 3: 'rd'}.get(count % 10, 'th')}"


def _result_hash(result: str | None, tool_name: str = "") -> str:
    parsed = safe_json_loads(result or "")
    if parsed is None:
        return _sha256(result or "")
    if tool_name == "execute_code":
        parsed = _without_execute_code_metadata(parsed)
    return _sha256(_canonical_json(parsed))


_BOOL_WORDS = {w: True for w in ("1", "true", "yes", "on", "enabled")} | {w: False for w in ("0", "false", "no", "off", "disabled")}


def _as_bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "on", "enabled"}:
            return True
        if lowered in {"0", "false", "no", "off", "disabled"}:
            return False
    return default


def _positive_int(value: Any, default: int) -> int:
    if value is None:
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed >= 1 else default


def _non_negative_int(value: Any, default: int) -> int:
    """Parse a session-cap value. 0 is a valid (disable) value; negatives and
    junk fall back to the default. Used for caps where 0 means DISABLE (e.g.
    the browser failure cap), unlike ``_positive_int``."""
    if value is None:
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed >= 0 else default


def _subagent_spawn_count(args: Mapping[str, Any]) -> int:
    """How many subagents a single delegate_task call spawns.

    delegate_task runs in one of two modes: a batch (``tasks`` is a non-empty
    list, one child per item) or a single task (``goal``). Count the batch size
    when present, otherwise 1, so the session subagent cap reflects real spawns
    rather than delegate_task invocations. Control actions (list/steer/stop)
    spawn nothing and must not consume the cap.
    """
    action = ""
    if isinstance(args, Mapping):
        action = str(args.get("action") or "").strip().lower()
    if action in {"list", "steer", "stop"}:
        return 0
    tasks = args.get("tasks") if isinstance(args, Mapping) else None
    if isinstance(tasks, list) and tasks:
        return len(tasks)
    return 1


def _sha256(value: str) -> str:
    # surrogatepass: tool results scraped from the web can carry unpaired
    # UTF-16 surrogates (e.g. half of a mathematical-bold pair); a strict
    # encode raises and takes down the whole conversation loop. The hash only
    # needs deterministic bytes, not valid UTF-8.
    return hashlib.sha256(value.encode("utf-8", "surrogatepass")).hexdigest()
