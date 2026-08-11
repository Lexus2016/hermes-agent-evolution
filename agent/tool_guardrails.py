"""Pure tool-call loop guardrail primitives.

The controller in this module is intentionally side-effect free: it tracks
per-turn tool-call observations and returns decisions. Runtime code owns whether
those decisions become warning guidance, synthetic tool results, or controlled
turn halts.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Mapping

from utils import safe_json_loads
from agent.tool_result_classification import file_mutation_result_landed

if TYPE_CHECKING:  # avoid a circular import; policy_interceptors imports this module
    from agent.policy_interceptors import PolicyInterceptorRegistry
    from agent.recheck_suppression import RecheckController


IDEMPOTENT_TOOL_NAMES = frozenset({
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

MUTATING_TOOL_NAMES = frozenset({
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

# #974/#969/#970 — tools whose retry spirals are the system's largest failure
# sources. Trace-miner evidence: terminal (1237 failures / 410 sessions),
# execute_code (59 failures / 14 sessions, max 17 consecutive retries),
# read_file (26 failures / 10 sessions with ≥5 consecutive reads). These tools
# get an always-on per-tool failure cap that halts regardless of
# ``hard_stop_enabled``, mirroring the browser_failure_cap pattern.
# #1141 — process added: an 18-deep process polling spiral was observed in
# production (11 failures, 1 session). process poll/wait loops that each
# "succeed" but never converge on a terminal state run uncapped without this.
# #1143 — search_files added: 27 consecutive search_files calls across 224
# sessions (190 failures) regressed from 15/8 — the agent reformulates
# patterns (glob vs regex, retries after empty results) without switching
# strategy. Cap consecutive search_files calls to force a strategy switch.
# #1185/#1186/#1187 — deferred-tool loading chain + memory added:
# tool_call (168 failures / 21 sessions, 13-deep spirals), memory
# (94 failures / 21 sessions, 11-deep, regressed 10x from #1135/#1136),
# tool_describe (59 failures, the search→describe→call middle step). These
# had no circuit breaker — the agent blind-retried the same failing call up
# to 13 consecutive times with no fallback. Extending the existing cap covers
# the whole deferred-tool chain consistently with the core tools.
_SPIRAL_PRONE_TOOLS = frozenset({
    "terminal",
    "execute_code",
    "read_file",
    "process",
    "search_files",
    "tool_call",
    "tool_describe",
    "memory",
    # #1258 — patch tool failures: 255 failures in 7d. Patch failures are
    # deterministic (no-match, ambiguous, identical-edit) — the model
    # retries the same stale old_string without re-reading the file. The
    # fallback directive ("use read_file to verify the exact text before
    # patching, or use write_file") is already defined at line 751.
    # Adding patch here activates the always-on spiral_failure_cap (default
    # 5) that halts the turn regardless of hard_stop_enabled, bounding the
    # retry spiral and forcing a strategy switch.
    "patch",
    # #1840 — write_file parse-errors: 48 failures in 7d with 15-deep
    # spirals. The fail-closed syntax gate returns a validation error
    # (JSON/YAML/TOML parse failure) that is deterministic — the same
    # malformed content will fail identically on retry. Adding write_file
    # here activates the always-on spiral_failure_cap (default 5) so the
    # agent is forced to fix the content instead of blind-retrying.
    "write_file",
})

# #1585 — number of consecutive successes required before a spiral-prone
# tool's cross-turn failure streak decays by 1. The production terminal
# spiral is fail, diagnostic-success (pwd, ls), fail, repeating — and the
# fallback directive actively recommends the diagnostic. With a 1-success
# decay that pattern nets 0 per cycle and the cap is unreachable. Requiring
# a sustained run means a single interspersed success does not drain the
# streak, so fail/succeed/fail climbs +1 per cycle toward the cap.
_SUCCESSES_TO_DECAY = 2


@dataclass(frozen=True)
class ToolCallGuardrailConfig:
    """Thresholds for per-turn tool-call loop detection.

    Warnings are enabled by default and never prevent tool execution. Hard stops
    are explicit opt-in so interactive CLI/TUI sessions get a gentle nudge unless
    the user enables circuit-breaker behavior in config.yaml.
    """

    warnings_enabled: bool = True
    hard_stop_enabled: bool = False
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
    per_tool_failure_caps: dict[str, int] = field(default_factory=lambda: {"memory": 3})
    spiral_prone_tools: frozenset[str] = field(
        default_factory=lambda: _SPIRAL_PRONE_TOOLS
    )
    idempotent_tools: frozenset[str] = field(
        default_factory=lambda: IDEMPOTENT_TOOL_NAMES
    )
    mutating_tools: frozenset[str] = field(default_factory=lambda: MUTATING_TOOL_NAMES)
    loop_caps: "LoopCapConfig" = field(default_factory=lambda: LoopCapConfig())

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any] | None) -> "ToolCallGuardrailConfig":
        """Build config from the `tool_loop_guardrails` config.yaml section."""
        if not isinstance(data, Mapping):
            return cls()

        warn_after = data.get("warn_after")
        if not isinstance(warn_after, Mapping):
            warn_after = {}
        hard_stop_after = data.get("hard_stop_after")
        if not isinstance(hard_stop_after, Mapping):
            hard_stop_after = {}

        defaults = cls()
        return cls(
            warnings_enabled=_as_bool(
                data.get("warnings_enabled"), defaults.warnings_enabled
            ),
            hard_stop_enabled=_as_bool(
                data.get("hard_stop_enabled"), defaults.hard_stop_enabled
            ),
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


@dataclass(frozen=True)
class ToolCallSignature:
    """Stable, non-reversible identity for a tool name plus canonical args."""

    tool_name: str
    args_hash: str

    @classmethod
    def from_call(
        cls, tool_name: str, args: Mapping[str, Any] | None
    ) -> "ToolCallSignature":
        canonical = canonical_tool_args(args or {})
        return cls(tool_name=tool_name, args_hash=_sha256(canonical))

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

    # Terminal and process: non-zero exit code is the canonical failure
    # signal. The process tool (action=poll/log/wait) returns a JSON dict
    # with exit_code but no "error" key when the background process exits
    # non-zero — without this check those results are misclassified as
    # successes and the spiral cap never fires (#1839).
    if tool_name in ("terminal", "process"):
        data = safe_json_loads(result)
        if isinstance(data, dict):
            exit_code = data.get("exit_code")
            if exit_code is not None and exit_code != 0:
                return True, f" [exit {exit_code}]"
            # #2241 — process-specific failure patterns that don't carry an
            # exit_code.  process_registry returns {"status": "not_found",
            # "error": "No process with ID …"} for session-not-found,
            # {"status": "error", "error": str(e)} for action failures, and
            # {"success": False, "error": …} for write/submit rejections.
            # Without these checks the early ``return False`` above swallows
            # them, the streak counter never accumulates, and the spiral cap
            # never fires — regressing to 18-deep (#2241).
            if tool_name == "process":
                status = data.get("status")
                if status in ("not_found", "error", "already_exited"):
                    return True, f" [{status}]"
                if data.get("success") is False:
                    return True, " [failed]"
                if data.get("error") and not data.get("output"):
                    return True, " [error]"
        return False, ""

    if tool_name == "memory":
        data = safe_json_loads(result)
        if isinstance(data, dict):
            if data.get("success") is False and "exceed the limit" in data.get(
                "error", ""
            ):
                return True, " [full]"

    # #1188 — mirror the bot_detection_warning check from _detect_tool_failure
    # so the fallback classifier never disagrees with the production path.
    data = safe_json_loads(result)
    if (
        isinstance(data, dict)
        and data.get("success") is True
        and isinstance(data.get("bot_detection_warning"), str)
    ):
        return True, " [bot detection]"

    lower = result[:500].lower()
    if '"error"' in lower or '"failed"' in lower or result.startswith("Error"):
        return True, " [error]"

    return False, ""


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
        self._no_progress: dict[ToolCallSignature, tuple[str, int]] = {}
        self._halt_decision: ToolGuardrailDecision | None = None
        # #1041 — last executed call, for immediate-recheck detection.
        self._last_signature: ToolCallSignature | None = None
        self._last_call_succeeded: bool = False
        if self.policy_registry is not None:
            self.policy_registry.reset_for_turn()
        # Per-turn runaway-loop cap counters. Reset every turn (this method
        # runs at the start of each run_conversation), so the caps bound a
        # single agent loop rather than accumulating across the session.
        self._turn_web_search_count = 0
        self._turn_subagent_count = 0

    @property
    def halt_decision(self) -> ToolGuardrailDecision | None:
        return self._halt_decision

    def before_call(
        self, tool_name: str, args: Mapping[str, Any] | None
    ) -> ToolGuardrailDecision:
        # Policy interceptors run first and apply regardless of hard_stop_enabled:
        # a denied policy is a deterministic user rule, not a loop limit.
        if self.policy_registry is not None and self.policy_registry.enabled:
            policy_decision = self.policy_registry.evaluate(tool_name, args)
            if not policy_decision.allows_execution:
                self._halt_decision = policy_decision
                return policy_decision

        signature = ToolCallSignature.from_call(tool_name, _coerce_args(args))

        # #1826 — session-level permanent hard-stop. Once a tool has been
        # session-hard-stopped, ALL subsequent calls are blocked unconditionally
        # for the remainder of the session. This is the unconditional ceiling:
        # it does not depend on error classification and cannot be bypassed by
        # interspersed successes or reset_for_turn. This is the structural fix
        # for the 5th recurrence of the terminal retry spiral.
        if tool_name in self._session_hard_stopped:
            directive = _fallback_directive_for(tool_name)
            if (
                _is_browser_tool(tool_name)
                and tool_name not in self.config.spiral_prone_tools
            ):
                code = "browser_tool_failure_cap"
            else:
                code = "session_hard_stop"
            decision = ToolGuardrailDecision(
                action="block",
                code=code,
                message=(
                    f"Blocked {tool_name}: it has been permanently halted for this "
                    f"session after reaching the retry cap. This failure pattern is "
                    "deterministic — retrying will not fix it. Use the fallback "
                    "directive below."
                ),
                tool_name=tool_name,
                count=self._cross_turn_tool_failure_counts.get(tool_name, 0),
                signature=signature,
                fallback_directive=directive,
            )
            self._halt_decision = decision
            return decision

        # Cross-turn spiral enforcement (#1109/#1110/#1111/#1112): if the
        # same tool has been failing across turns and the cross-turn streak
        # has already reached the cap, block execution immediately — BEFORE
        # the hard_stop_enabled gate.  This makes the browser/spiral caps
        # truly always-on: the model gets a synthetic blocked result with
        # the fallback directive instead of being allowed to execute the
        # same failing call again.  reset_for_turn clears _halt_decision but
        # NOT _cross_turn_tool_failure_counts, so the streak survives.
        cross_turn_count = self._cross_turn_tool_failure_counts.get(tool_name, 0)
        effective_spiral_cap = self._effective_cap_for(tool_name)
        if cross_turn_count >= 1 and (
            (
                effective_spiral_cap >= 1
                and tool_name in self.config.spiral_prone_tools
                and cross_turn_count >= effective_spiral_cap
            )
            or (
                self.config.browser_failure_cap >= 1
                and _is_browser_tool(tool_name)
                and cross_turn_count >= self.config.browser_failure_cap
            )
        ):
            directive = _fallback_directive_for(tool_name)
            if (
                _is_browser_tool(tool_name)
                and tool_name not in self.config.spiral_prone_tools
            ):
                code = "browser_tool_failure_cap"
                cap = self.config.browser_failure_cap
            else:
                code = "spiral_prone_tool_failure_cap"
                cap = effective_spiral_cap
            decision = ToolGuardrailDecision(
                action="block",
                code=code,
                message=(
                    f"Blocked {tool_name}: it has failed {cross_turn_count} times across "
                    f"recent turns, reaching the retry cap ({cap}). This failure pattern "
                    "is deterministic — retrying the same way will not fix it. "
                    "Use the fallback directive below."
                ),
                tool_name=tool_name,
                count=cross_turn_count,
                signature=signature,
                fallback_directive=directive,
            )
            self._halt_decision = decision
            return decision

        # #1041 — recheck suppression. Config-gated via ``recheck_controller``
        # (None/disabled by default -> skipped). Suppresses a single immediate
        # identical repeat of a successful read-only call as a redundant
        # self-verification, returning a non-halting ``suppress`` decision
        # (allows_execution False, should_halt False) so the one call is skipped
        # but the turn continues. Every idempotent-call decision is logged for
        # calibration.
        if (
            self.recheck_controller is not None
            and self.recheck_controller.enabled
            and self._is_idempotent(tool_name)
        ):
            is_immediate_repeat = (
                self._last_signature is not None
                and signature == self._last_signature
                and self._last_call_succeeded
            )
            suppress, _result = self.recheck_controller.decide(
                tool_name,
                is_idempotent=True,
                is_immediate_repeat=is_immediate_repeat,
                prior_succeeded=self._last_call_succeeded,
            )
            if suppress:
                return ToolGuardrailDecision(
                    action="suppress",
                    code="recheck_suppressed",
                    message=(
                        f"Suppressed {tool_name}: this read-only call is an immediate "
                        "repeat of one that just succeeded with identical arguments. "
                        "Reuse the previous result instead of re-verifying it."
                    ),
                    tool_name=tool_name,
                    count=0,
                    signature=signature,
                )
        # ── Per-turn runaway-loop caps ──────────────────────────────────
        # These are hard ceilings on how many times a runaway-prone tool may
        # be called within a single agent loop (turn). They apply regardless
        # of hard_stop_enabled (which only governs the per-turn loop detector).
        # We block BEFORE the call runs once the count is already at the cap,
        # then increment for an allowed call so the (cap+1)-th is refused.
        cap_block = self._check_loop_cap(tool_name, _coerce_args(args), signature)
        if cap_block is not None:
            return cap_block

        if not self.config.hard_stop_enabled:
            return ToolGuardrailDecision(tool_name=tool_name, signature=signature)

        exact_count = self._exact_failure_counts.get(signature, 0)
        if exact_count >= self.config.exact_failure_block_after:
            decision = ToolGuardrailDecision(
                action="block",
                code="repeated_exact_failure_block",
                message=(
                    f"Blocked {tool_name}: the same tool call failed {exact_count} "
                    "times with identical arguments. Stop retrying it unchanged; "
                    "change strategy or explain the blocker."
                ),
                tool_name=tool_name,
                count=exact_count,
                signature=signature,
            )
            self._halt_decision = decision
            return decision

        if self._is_idempotent(tool_name):
            record = self._no_progress.get(signature)
            if record is not None:
                _result_hash, repeat_count = record
                if repeat_count >= self.config.no_progress_block_after:
                    decision = ToolGuardrailDecision(
                        action="block",
                        code="idempotent_no_progress_block",
                        message=(
                            f"Blocked {tool_name}: this read-only call returned the same "
                            f"result {repeat_count} times. Stop repeating it unchanged; "
                            "use the result already provided or try a different query."
                        ),
                        tool_name=tool_name,
                        count=repeat_count,
                        signature=signature,
                    )
                    self._halt_decision = decision
                    return decision

        return ToolGuardrailDecision(tool_name=tool_name, signature=signature)

    def after_call(
        self,
        tool_name: str,
        args: Mapping[str, Any] | None,
        result: str | None,
        *,
        failed: bool | None = None,
    ) -> ToolGuardrailDecision:
        args = _coerce_args(args)
        signature = ToolCallSignature.from_call(tool_name, args)
        if failed is None:
            failed, _ = classify_tool_failure(tool_name, result)

        # #1041 — record the last executed call so the next before_call can
        # detect an immediate identical recheck of a successful read-only call.
        self._last_signature = signature
        self._last_call_succeeded = not failed

        # Feed the per-turn observation ledger so ordering-aware policy
        # interceptors (e.g. read-before-write) can see prior calls.
        if self.policy_registry is not None and self.policy_registry.enabled:
            self.policy_registry.record_observation(tool_name, args, failed=failed)

        if failed:
            exact_count = self._exact_failure_counts.get(signature, 0) + 1
            self._exact_failure_counts[signature] = exact_count
            self._no_progress.pop(signature, None)

            same_count = self._same_tool_failure_counts.get(tool_name, 0) + 1
            self._same_tool_failure_counts[tool_name] = same_count

            # Cross-turn accumulation: the same tool failing once per turn
            # is the dominant spiral pattern.  The per-turn counter resets
            # each API turn, so without this cross-turn tracker the cap
            # only catches rare within-turn spirals (multiple calls in one
            # tool batch).  Here we carry the streak forward.
            cross_turn_count = (
                self._cross_turn_tool_failure_counts.get(tool_name, 0) + 1
            )
            self._cross_turn_tool_failure_counts[tool_name] = cross_turn_count
            # #1585 — a failure breaks the consecutive-success chain, so the
            # sustained-recovery counter restarts from zero.
            self._cross_turn_success_streaks.pop(tool_name, None)

            # Effective streak is the max of per-turn and cross-turn counts.
            # Within-turn spirals (5 calls in one batch) still trip the cap
            # via the per-turn count; cross-turn spirals (1 call/turn for 5
            # turns) trip it via the cross-turn count.
            effective_streak = max(same_count, cross_turn_count)

            # #745 — browser tools get an always-on per-tool failure cap that
            # halts REGARDLESS of ``hard_stop_enabled``. Browser retries are
            # expensive and their deterministic failures (CDP down, nav timeout,
            # missing tool) do not recover on a blind retry, so bound the spiral
            # even in the default hard-stop-off mode. This mirrors the per-URL
            # cap in ``tools/browser_navigate_fallback`` and does NOT change the
            # generic hard_stop circuit breaker for native tools below.
            if (
                self.config.browser_failure_cap >= 1
                and _is_browser_tool(tool_name)
                and effective_streak >= self.config.browser_failure_cap
            ):
                # #1826 — session-hard-stop browser tools too so they don't
                # recur after interspersed successes on a different page.
                self._session_hard_stopped.add(tool_name)
                decision = ToolGuardrailDecision(
                    action="halt",
                    code="browser_tool_failure_cap",
                    message=(
                        f"Stopped {tool_name}: it failed {effective_streak} times, "
                        f"reaching the browser retry cap ({self.config.browser_failure_cap}). "
                        "Browser retries are expensive and this failure is deterministic — "
                        "stop re-driving the browser and use the fallback."
                    ),
                    tool_name=tool_name,
                    count=effective_streak,
                    signature=signature,
                    fallback_directive=_fallback_directive_for(tool_name),
                )
                self._halt_decision = decision
                return decision

            # #974/#969/#970 — terminal, execute_code, and read_file are the
            # system's largest failure sources. Their retry spirals persist
            # because the loop_guard's fallback_directive is advisory (the
            # agent ignores it and retries). This always-on cap halts the
            # turn after N consecutive same-tool failures REGARDLESS of
            # ``hard_stop_enabled``, mirroring the browser_failure_cap pattern.
            # The fallback_directive gives the agent a concrete alternative.
            if (
                self._effective_cap_for(tool_name) >= 1
                and tool_name in self.config.spiral_prone_tools
                and effective_streak >= self._effective_cap_for(tool_name)
            ):
                # #1826 — permanently session-hard-stop this tool so subsequent
                # turns cannot retry it. The cross-turn streak alone was
                # insufficient because interspersed diagnostic successes (pwd,
                # ls) decaying the streak allowed the spiral to recur across
                # turns. The session stop is irreversible for this session.
                self._session_hard_stopped.add(tool_name)
                _cap = self._effective_cap_for(tool_name)
                directive = _fallback_directive_for(tool_name)
                decision = ToolGuardrailDecision(
                    action="halt",
                    code="spiral_prone_tool_failure_cap",
                    message=(
                        f"Stopped {tool_name}: it failed {effective_streak} times, "
                        f"reaching the retry cap ({_cap}). "
                        "This failure pattern is deterministic — retrying the same way "
                        "will not fix it. Use the fallback directive below."
                    ),
                    tool_name=tool_name,
                    count=effective_streak,
                    signature=signature,
                    fallback_directive=directive,
                )
                self._halt_decision = decision
                return decision

            if (
                self.config.hard_stop_enabled
                and effective_streak >= self.config.same_tool_failure_halt_after
            ):
                decision = ToolGuardrailDecision(
                    action="halt",
                    code="same_tool_failure_halt",
                    message=(
                        f"Stopped {tool_name}: it failed {effective_streak} times. "
                        "Stop retrying the same failing tool path and choose a different approach."
                    ),
                    tool_name=tool_name,
                    count=effective_streak,
                    signature=signature,
                    fallback_directive=_fallback_directive_for(tool_name),
                )
                self._halt_decision = decision
                return decision

            if (
                self.config.warnings_enabled
                and exact_count >= self.config.exact_failure_warn_after
            ):
                return ToolGuardrailDecision(
                    action="warn",
                    code="repeated_exact_failure_warning",
                    message=(
                        f"{tool_name} has failed {exact_count} times with identical arguments. "
                        "This looks like a loop; inspect the error and change strategy "
                        "instead of retrying it unchanged."
                    ),
                    tool_name=tool_name,
                    count=exact_count,
                    signature=signature,
                    fallback_directive=_fallback_directive_for(tool_name),
                )

            if (
                self.config.warnings_enabled
                and effective_streak >= self.config.same_tool_failure_warn_after
            ):
                return ToolGuardrailDecision(
                    action="warn",
                    code="same_tool_failure_warning",
                    message=_tool_failure_recovery_hint(tool_name, effective_streak),
                    tool_name=tool_name,
                    count=effective_streak,
                    signature=signature,
                    fallback_directive=_fallback_directive_for(tool_name),
                )

            return ToolGuardrailDecision(
                tool_name=tool_name, count=exact_count, signature=signature
            )

        self._exact_failure_counts.pop(signature, None)
        self._same_tool_failure_counts.pop(tool_name, None)
        # #1188 — decay (decrement) the cross-turn streak instead of clearing
        # it. A single success does NOT prove the browser backend recovered:
        # intermittent successes (navigating to a different, fast-loading
        # page between failing attempts) kept the old pop() from ever
        # reaching the cap, allowing 15-deep spirals. Decay by 1 per success
        # so a genuinely recovered backend (several successes in a row)
        # drains the streak back to 0, but a failure/success/failure pattern
        # still accumulates toward the cap and eventually halts.
        #
        # #1585 — for spiral-prone tools, the simple 1-success decay was
        # insufficient: the production terminal spiral is fail → diagnostic-
        # success (pwd, ls — the fallback directive's own advice) → fail,
        # which nets 0 per cycle and never reaches the cap. Now we require a
        # SUSTAINED run of successes (_SUCCESSES_TO_DECAY, default 2) before
        # draining the streak by 1. A single interspersed success increments
        # the success-streak but doesn't drain the failure streak, so the
        # fail/succeed/fail pattern nets +1 per cycle and climbs toward the
        # cap. A genuine recovery (N consecutive successes) drains it fully.
        if tool_name in self._cross_turn_tool_failure_counts:
            current = self._cross_turn_tool_failure_counts[tool_name]
            is_spiral_prone = tool_name in self.config.spiral_prone_tools
            # #1826 — once session-hard-stopped, the streak is FROZEN. Decay
            # would allow the tool back into the rotation and re-open the
            # spiral. The permanent stop is the whole point.
            if tool_name in self._session_hard_stopped:
                pass  # no decay — session stop is irreversible
            elif is_spiral_prone:
                succ = self._cross_turn_success_streaks.get(tool_name, 0) + 1
                if succ >= _SUCCESSES_TO_DECAY:
                    # Sustained recovery — drain the failure streak by 1.
                    if current <= 1:
                        self._cross_turn_tool_failure_counts.pop(tool_name, None)
                    else:
                        self._cross_turn_tool_failure_counts[tool_name] = current - 1
                    # Reset the success streak so the next decay needs another
                    # sustained run (not a single additional success).
                    self._cross_turn_success_streaks.pop(tool_name, None)
                else:
                    # Not enough consecutive successes yet — remember the
                    # streak but don't drain the failure count.
                    self._cross_turn_success_streaks[tool_name] = succ
            elif current <= 1:
                self._cross_turn_tool_failure_counts.pop(tool_name, None)
            else:
                self._cross_turn_tool_failure_counts[tool_name] = current - 1

        if not self._is_idempotent(tool_name):
            self._no_progress.pop(signature, None)
            return ToolGuardrailDecision(tool_name=tool_name, signature=signature)

        result_hash = _result_hash(result)
        previous = self._no_progress.get(signature)
        repeat_count = 1
        if previous is not None and previous[0] == result_hash:
            repeat_count = previous[1] + 1
        self._no_progress[signature] = (result_hash, repeat_count)

        if (
            self.config.warnings_enabled
            and repeat_count >= self.config.no_progress_warn_after
        ):
            return ToolGuardrailDecision(
                action="warn",
                code="idempotent_no_progress_warning",
                message=(
                    f"{tool_name} returned the same result {repeat_count} times. "
                    "Use the result already provided or change the query instead of "
                    "repeating it unchanged."
                ),
                tool_name=tool_name,
                count=repeat_count,
                signature=signature,
            )

        return ToolGuardrailDecision(
            tool_name=tool_name, count=repeat_count, signature=signature
        )

    def _is_idempotent(self, tool_name: str) -> bool:
        if tool_name in self.config.mutating_tools:
            return False
        return tool_name in self.config.idempotent_tools

    def _effective_cap_for(self, tool_name: str) -> int:
        """Return the effective spiral-failure cap for *tool_name*.

        #1825 — per-tool overrides (e.g. memory=3) take precedence over the
        default ``spiral_failure_cap`` (5). Non-spiral tools return the
        default cap but are filtered by membership checks at call sites.
        """
        caps = self.config.per_tool_failure_caps
        if tool_name in caps and caps[tool_name] >= 1:
            return caps[tool_name]
        return self.config.spiral_failure_cap

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


def _result_hash(result: str | None) -> str:
    parsed = safe_json_loads(result or "")
    if parsed is not None:
        try:
            canonical = json.dumps(
                parsed,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            )
        except TypeError:
            canonical = str(parsed)
    else:
        canonical = result or ""
    return _sha256(canonical)


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
    rather than delegate_task invocations.
    """
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
