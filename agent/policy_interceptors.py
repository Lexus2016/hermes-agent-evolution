"""Pluggable, deterministic per-policy tool-call interceptors.

This module adds a user-authorable *policy* layer on top of the loop/limit
guardrails in :mod:`agent.tool_guardrails`. Where the loop guardrails answer
"is the model stuck repeating the same call?", policy interceptors answer
"does this specific call satisfy the user's deterministic rules?" — e.g.
"read a file before you write it", or "never run this command".

Design contract (mirrors ``tool_guardrails`` intentionally):

* Pure and side-effect free. A :class:`PolicyInterceptor` inspects an
  immutable :class:`ToolCallContext` and returns a :class:`PolicyOutcome`
  (allow / deny / rewrite). It never executes the tool or mutates state.
* Deterministic. Same context + same per-turn observation ledger always
  yields the same outcome. No clocks, no randomness, no network.
* Decisions are expressed as :class:`agent.tool_guardrails.ToolGuardrailDecision`
  so the existing dispatch path (``before_call`` → ``allows_execution``)
  consumes them with zero new wiring.

The registry is config-driven via the ``policy_interceptors`` section of
``config.yaml``. Named, built-in policies are enabled/parametrized by config;
the loop guardrails continue to run alongside them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

from agent.tool_guardrails import ToolCallSignature, ToolGuardrailDecision


@dataclass(frozen=True)
class ToolCallContext:
    """Immutable view of a single tool call presented to a policy.

    ``observations`` is the per-turn ledger of prior *successful* tool calls
    (see :class:`ToolCallObservation`). Policies that enforce ordering rules
    (read-before-write) read from it; stateless policies ignore it.
    """

    tool_name: str
    args: Mapping[str, Any]
    signature: ToolCallSignature
    observations: tuple["ToolCallObservation", ...] = ()


@dataclass(frozen=True)
class ToolCallObservation:
    """A prior tool call recorded this turn, for ordering-aware policies."""

    tool_name: str
    args: Mapping[str, Any]
    failed: bool


@dataclass(frozen=True)
class PolicyOutcome:
    """Result of evaluating one policy against one tool call.

    * ``allow``   — policy has no objection (the default no-op outcome).
    * ``deny``    — block execution; ``message`` explains the recoverable fix.
    * ``rewrite`` — allow, but execute with ``rewritten_args`` instead.
    """

    action: str = "allow"  # allow | deny | rewrite
    message: str = ""
    rewritten_args: Mapping[str, Any] | None = None

    @classmethod
    def allow(cls) -> "PolicyOutcome":
        return cls()

    @classmethod
    def deny(cls, message: str) -> "PolicyOutcome":
        return cls(action="deny", message=message)

    @classmethod
    def rewrite(cls, args: Mapping[str, Any], message: str = "") -> "PolicyOutcome":
        return cls(action="rewrite", rewritten_args=dict(args), message=message)


# A policy is a pure function from a context to an outcome.
PolicyInterceptor = Callable[[ToolCallContext], PolicyOutcome]


@dataclass(frozen=True)
class RegisteredPolicy:
    """A built-in policy bound to its config-supplied name."""

    name: str
    interceptor: PolicyInterceptor


class PolicyInterceptorRegistry:
    """Ordered set of enabled policy interceptors for one agent turn.

    Evaluation is first-match-wins on ``deny``: the registry returns the first
    denying policy's decision. ``rewrite`` outcomes are composed in order so a
    later policy sees the args produced by an earlier one. The combined result
    is a :class:`ToolGuardrailDecision`, so callers reuse the exact same
    ``allows_execution`` / ``should_halt`` contract as the loop guardrails.
    """

    def __init__(self, policies: list[RegisteredPolicy] | None = None):
        self._policies: list[RegisteredPolicy] = list(policies or [])
        self._observations: list[ToolCallObservation] = []

    @property
    def enabled(self) -> bool:
        return bool(self._policies)

    @property
    def policy_names(self) -> tuple[str, ...]:
        return tuple(p.name for p in self._policies)

    def reset_for_turn(self) -> None:
        """Clear the per-turn observation ledger (mirrors the controller)."""
        self._observations = []

    def record_observation(
        self, tool_name: str, args: Mapping[str, Any] | None, *, failed: bool
    ) -> None:
        """Record a completed tool call so ordering-aware policies can see it."""
        self._observations.append(
            ToolCallObservation(
                tool_name=tool_name,
                args=_coerce_args(args),
                failed=bool(failed),
            )
        )

    def evaluate(
        self, tool_name: str, args: Mapping[str, Any] | None
    ) -> ToolGuardrailDecision:
        """Run every enabled policy; return the combined guardrail decision.

        Returns an ``allow`` decision (carrying possibly-rewritten args in
        ``signature``) when no policy denies. Returns a ``block`` decision on
        the first deny.
        """
        current_args = _coerce_args(args)
        signature = ToolCallSignature.from_call(tool_name, current_args)
        observations = tuple(self._observations)

        for policy in self._policies:
            context = ToolCallContext(
                tool_name=tool_name,
                args=current_args,
                signature=ToolCallSignature.from_call(tool_name, current_args),
                observations=observations,
            )
            outcome = policy.interceptor(context)

            if outcome.action == "deny":
                return ToolGuardrailDecision(
                    action="block",
                    code=f"policy_deny:{policy.name}",
                    message=outcome.message
                    or f"Blocked {tool_name}: denied by policy '{policy.name}'.",
                    tool_name=tool_name,
                    signature=ToolCallSignature.from_call(tool_name, current_args),
                )

            if outcome.action == "rewrite" and outcome.rewritten_args is not None:
                current_args = _coerce_args(outcome.rewritten_args)

        signature = ToolCallSignature.from_call(tool_name, current_args)
        return ToolGuardrailDecision(tool_name=tool_name, signature=signature)


# ── Built-in policies ────────────────────────────────────────────────────


def make_require_read_before_write(
    write_tools: frozenset[str],
    read_tools: frozenset[str],
    path_keys: tuple[str, ...],
) -> PolicyInterceptor:
    """Deny a write to a path that has not been successfully read this turn.

    Implements the issue's headline success criterion: a write-before-read
    sequence is rejected before execution with a clear, recoverable message.
    A write whose path can't be determined is allowed (fail-open) so the
    policy never blocks calls it can't reason about.
    """

    def policy(ctx: ToolCallContext) -> PolicyOutcome:
        if ctx.tool_name not in write_tools:
            return PolicyOutcome.allow()

        target = _extract_path(ctx.args, path_keys)
        if target is None:
            return PolicyOutcome.allow()

        for obs in ctx.observations:
            if obs.failed or obs.tool_name not in read_tools:
                continue
            if _extract_path(obs.args, path_keys) == target:
                return PolicyOutcome.allow()

        return PolicyOutcome.deny(
            f"Blocked {ctx.tool_name} on '{target}': policy requires reading a "
            "file before writing it. Read the file first (e.g. with read_file), "
            "then retry the write."
        )

    return policy


def make_deny_tools(denied: frozenset[str]) -> PolicyInterceptor:
    """Deny any call to a named tool outright (an allowlist's complement)."""

    def policy(ctx: ToolCallContext) -> PolicyOutcome:
        if ctx.tool_name in denied:
            return PolicyOutcome.deny(
                f"Blocked {ctx.tool_name}: this tool is disabled by policy."
            )
        return PolicyOutcome.allow()

    return policy


# Built-in policy factories keyed by the config ``policy`` field. Each factory
# turns a config mapping into a concrete :class:`PolicyInterceptor`.
DEFAULT_WRITE_TOOLS = frozenset({"write_file", "patch"})
DEFAULT_READ_TOOLS = frozenset({"read_file"})
DEFAULT_PATH_KEYS = ("path", "file_path", "filename")

PolicyFactory = Callable[[Mapping[str, Any]], PolicyInterceptor]


def _build_require_read_before_write(options: Mapping[str, Any]) -> PolicyInterceptor:
    return make_require_read_before_write(
        write_tools=_frozenset_opt(options.get("write_tools"), DEFAULT_WRITE_TOOLS),
        read_tools=_frozenset_opt(options.get("read_tools"), DEFAULT_READ_TOOLS),
        path_keys=_tuple_opt(options.get("path_keys"), DEFAULT_PATH_KEYS),
    )


def _build_deny_tools(options: Mapping[str, Any]) -> PolicyInterceptor:
    return make_deny_tools(_frozenset_opt(options.get("tools"), frozenset()))


BUILTIN_POLICY_FACTORIES: dict[str, PolicyFactory] = {
    "require_read_before_write": _build_require_read_before_write,
    "deny_tools": _build_deny_tools,
}


def build_registry_from_config(
    data: Mapping[str, Any] | None,
) -> PolicyInterceptorRegistry:
    """Build a registry from the ``policy_interceptors`` config section.

    Expected shape::

        policy_interceptors:
          enabled: true
          policies:
            - name: read-before-write     # optional label; defaults to policy id
              policy: require_read_before_write
              options: {}                 # optional, policy-specific
            - policy: deny_tools
              options: {tools: ["process"]}

    Unknown ``policy`` ids and malformed entries are skipped (fail-open). When
    ``enabled`` is false or absent the registry is empty and inert.
    """
    if not isinstance(data, Mapping):
        return PolicyInterceptorRegistry()
    if not _as_bool(data.get("enabled"), False):
        return PolicyInterceptorRegistry()

    raw_policies = data.get("policies")
    if not isinstance(raw_policies, (list, tuple)):
        return PolicyInterceptorRegistry()

    registered: list[RegisteredPolicy] = []
    for entry in raw_policies:
        if not isinstance(entry, Mapping):
            continue
        policy_id = entry.get("policy")
        if not isinstance(policy_id, str):
            continue
        factory = BUILTIN_POLICY_FACTORIES.get(policy_id)
        if factory is None:
            continue
        options = entry.get("options")
        if not isinstance(options, Mapping):
            options = {}
        name = entry.get("name")
        if not isinstance(name, str) or not name:
            name = policy_id
        registered.append(RegisteredPolicy(name=name, interceptor=factory(options)))

    return PolicyInterceptorRegistry(registered)


# ── Helpers ────────────────────────────────────────────────────────────────


def _extract_path(args: Mapping[str, Any], path_keys: tuple[str, ...]) -> str | None:
    for key in path_keys:
        value = args.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _coerce_args(args: Mapping[str, Any] | None) -> Mapping[str, Any]:
    return args if isinstance(args, Mapping) else {}


def _frozenset_opt(value: Any, default: frozenset[str]) -> frozenset[str]:
    if isinstance(value, (list, tuple, set, frozenset)):
        items = {str(item) for item in value if isinstance(item, str)}
        return frozenset(items) if items else default
    return default


def _tuple_opt(value: Any, default: tuple[str, ...]) -> tuple[str, ...]:
    if isinstance(value, (list, tuple)):
        items = tuple(str(item) for item in value if isinstance(item, str))
        return items or default
    return default


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
