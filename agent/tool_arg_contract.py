"""Deterministic tool-argument contract enforcement.

Issue #904 ("From Prompts to Contracts"). Every tool ships a JSON Schema
(the ``parameters`` block of its definition) declaring which arguments are
``required`` and which are constrained to an ``enum`` of allowed values.
That schema is sent to the model as part of its contract — but until now
nothing in the dispatch path actually re-checked a call against it before
running the tool handler. A call missing a required argument, or using a
value outside a declared ``enum``, either silently reaches a handler that
happens to tolerate it (inconsistent, handler by handler) or raises deep
inside the handler and gets flattened by
:meth:`tools.registry.ToolRegistry.dispatch`'s catch-all ``except Exception``
into a generic ``Tool execution failed: KeyError: '...'`` message — which
tells the model *that* something broke but not *what contract* it broke or
*how* to fix the call.

This module moves that contract from "documentation the model may or may
not honor" into code: :func:`check_tool_args_contract` re-checks a call's
final arguments against the tool's own registered schema right at the
composition boundary — after argument coercion and request middleware have
run, and before :meth:`tools.registry.ToolRegistry.dispatch` invokes the
handler — and returns a structured, deterministic verdict instead of
letting an under-specified call reach the tool at all.

Design mirrors :mod:`agent.verify_policy` / :mod:`agent.policy_interceptors`
intentionally: frozen dataclasses, a pure check function, opt-in via an
env var / config flag (default **OFF** — see :func:`tool_arg_contract_enabled`),
fail-open whenever the tool has no schema or the schema is malformed. Only
``required`` presence and ``enum`` membership are checked; this is
deliberately narrower than full JSON Schema validation (no type/format/
min-max/pattern checks) — type mismatches are already handled by coercion
in ``model_tools.coerce_tool_args``, which runs earlier in the same
composition boundary and is intentionally forgiving rather than rejecting.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Mapping, Optional, Tuple


@dataclass(frozen=True)
class ArgContractViolation:
    """One concrete way a tool call's arguments failed to satisfy the schema."""

    kind: str  # "missing_required" | "invalid_enum"
    param: str
    detail: str

    @classmethod
    def missing_required(cls, param: str) -> "ArgContractViolation":
        return cls(
            kind="missing_required",
            param=param,
            detail=f"missing required parameter '{param}'",
        )

    @classmethod
    def invalid_enum(
        cls, param: str, value: Any, allowed: Tuple[Any, ...]
    ) -> "ArgContractViolation":
        allowed_repr = ", ".join(repr(v) for v in allowed)
        return cls(
            kind="invalid_enum",
            param=param,
            detail=f"'{param}'={value!r} is not one of the allowed values: {allowed_repr}",
        )


@dataclass(frozen=True)
class ArgContractOutcome:
    """Result of checking one tool call's arguments against its schema."""

    tool_name: str
    violations: Tuple[ArgContractViolation, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.violations

    def error_message(self) -> str:
        """Render one actionable error string covering every violation."""
        details = "; ".join(v.detail for v in self.violations)
        return f"Invalid arguments for '{self.tool_name}': {details}."


def _allows_null(schema: Any) -> bool:
    """Return True when a JSON Schema fragment explicitly permits null.

    Small, self-contained duplicate of ``model_tools._schema_allows_null``
    (kept local so this module stays dependency-free like its siblings
    ``agent.verify_policy`` / ``agent.policy_interceptors``). Only used to
    avoid flagging a ``required`` parameter that the schema itself declares
    nullable — a call that explicitly passes ``None`` for such a parameter
    is satisfying the contract, not violating it.
    """
    if not isinstance(schema, Mapping):
        return False
    schema_type = schema.get("type")
    if schema_type == "null":
        return True
    if isinstance(schema_type, list) and "null" in schema_type:
        return True
    if schema.get("nullable") is True:
        return True
    for union_key in ("anyOf", "oneOf"):
        variants = schema.get(union_key)
        if not isinstance(variants, list):
            continue
        for variant in variants:
            if isinstance(variant, Mapping) and variant.get("type") == "null":
                return True
    return False


def check_tool_args_contract(
    tool_name: str,
    args: Mapping[str, Any],
    schema: Optional[Mapping[str, Any]],
) -> ArgContractOutcome:
    """Check *args* against *schema*'s ``required``/``enum`` contract.

    Fail-open by design: a missing/malformed schema, or a schema with no
    ``parameters``/``properties``, always yields ``ok``. This never invents
    a stricter contract than the tool itself declared, and it never rejects
    a param it doesn't recognize (unknown keys are the coercion layer's
    concern, not this one's).
    """
    if not isinstance(args, Mapping):
        args = {}
    if not isinstance(schema, Mapping):
        return ArgContractOutcome(tool_name=tool_name)
    parameters = schema.get("parameters")
    if not isinstance(parameters, Mapping):
        return ArgContractOutcome(tool_name=tool_name)
    properties = parameters.get("properties")
    if not isinstance(properties, Mapping):
        properties = {}

    violations: list[ArgContractViolation] = []

    required = parameters.get("required")
    if isinstance(required, (list, tuple)):
        for param in required:
            if not isinstance(param, str):
                continue
            if param in args and args.get(param) is not None:
                continue
            if param in args and _allows_null(properties.get(param)):
                continue  # explicit None on a nullable required field is fine
            violations.append(ArgContractViolation.missing_required(param))

    for param, prop_schema in properties.items():
        if param not in args or args.get(param) is None:
            continue
        if not isinstance(prop_schema, Mapping):
            continue
        allowed = prop_schema.get("enum")
        if not isinstance(allowed, (list, tuple)) or not allowed:
            continue
        value = args.get(param)
        if value not in allowed:
            violations.append(
                ArgContractViolation.invalid_enum(param, value, tuple(allowed))
            )

    return ArgContractOutcome(tool_name=tool_name, violations=tuple(violations))


_TOOL_ARG_CONTRACT_ENV = "HERMES_TOOL_ARG_CONTRACT"


def tool_arg_contract_enabled() -> bool:
    """Whether deterministic tool-argument contract enforcement is active.

    Default **OFF**. Enabled by setting ``HERMES_TOOL_ARG_CONTRACT`` to a
    truthy value (``1``/``true``/``yes``/``on``), or via the
    ``tool_arg_contract.enabled`` key in ``config.yaml``. Reads the env var
    first so a session can flip it without editing config. Any failure
    resolving config -> OFF (safe default). Mirrors
    :func:`agent.verify_policy.verify_policy_enabled` exactly.
    """
    env = os.environ.get(_TOOL_ARG_CONTRACT_ENV)
    if env is not None:
        return env.strip().lower() in {"1", "true", "yes", "on"}
    try:
        from hermes_cli.config import load_config as _load_config

        cfg = _load_config() or {}
    except Exception:
        return False
    section = cfg.get("tool_arg_contract") if isinstance(cfg, dict) else None
    if isinstance(section, dict) and "enabled" in section:
        return bool(section.get("enabled"))
    return False


__all__ = [
    "ArgContractViolation",
    "ArgContractOutcome",
    "check_tool_args_contract",
    "tool_arg_contract_enabled",
]
