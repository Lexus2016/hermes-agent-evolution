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
intentionally: frozen dataclasses, a pure check function, opt-out via an
env var / config flag (default **ON** since #1530 — see
:func:`tool_arg_contract_enabled`), fail-open whenever the tool has no schema
or the schema is malformed. Only ``required`` presence, ``enum`` membership,
and basic ``type`` matching (string, integer, number, boolean, array, object)
are checked; this is deliberately narrower than full JSON Schema validation (no
format/min-max/pattern checks). The type check mirrors
:func:`tools.tool_search._check_type` so native tools get the same guard
already available to discovered tools.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Mapping, Optional, Tuple


@dataclass(frozen=True)
class ArgContractViolation:
    """One concrete way a tool call's arguments failed to satisfy the schema."""

    kind: str  # "missing_required" | "invalid_enum" | "type_mismatch"
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

    @classmethod
    def type_mismatch(
        cls, param: str, value: Any, expected: str, got: str
    ) -> "ArgContractViolation":
        return cls(
            kind="type_mismatch",
            param=param,
            detail=(f"'{param}' has wrong type: expected {expected}, got {got}"),
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


# Map JSON Schema type strings to Python types for validation. ``number``
# accepts both int and float (JSON ints are a subset of floats). Mirrors
# ``tools.tool_search._SCHEMA_PY_TYPES`` — kept local so this module stays
# dependency-free like its siblings ``agent.verify_policy`` /
# ``agent.policy_interceptors``.
_SCHEMA_PY_TYPES: dict[str, tuple[type, ...]] = {
    "string": (str,),
    "integer": (int,),
    "number": (int, float),
    "boolean": (bool,),
    "array": (list, tuple),
    "object": (dict,),
}


def _check_type(value: Any, type_str: str) -> bool:
    """Check whether *value* matches the JSON Schema *type_str*.

    Returns ``True`` for unknown types (fail-open — don't block dispatch
    on a type we don't recognize). ``bool`` is a subclass of ``int`` in
    Python, so it is explicitly rejected for ``integer`` params.
    """
    if type_str == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    py_types = _SCHEMA_PY_TYPES.get(type_str)
    if py_types is None:
        return True  # unknown type — don't block dispatch
    return isinstance(value, py_types)


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

    # Type checking — validate basic types (string, integer, number, boolean,
    # array, object) against the schema's ``type`` declaration. Mirrors
    # ``tools.tool_search.validate_tool_args`` for native tools. Skips
    # params that are absent or None (null is acceptable for optional params).
    for param, prop_schema in properties.items():
        if param not in args or args.get(param) is None:
            continue
        if not isinstance(prop_schema, Mapping):
            continue
        declared_type = prop_schema.get("type")
        if not declared_type:
            continue
        value = args.get(param)
        if isinstance(declared_type, str):
            type_variants = [declared_type]
        elif isinstance(declared_type, list):
            type_variants = declared_type
        else:
            continue
        if not any(_check_type(value, t) for t in type_variants):
            violations.append(
                ArgContractViolation.type_mismatch(
                    param,
                    value,
                    " or ".join(str(t) for t in type_variants),
                    type(value).__name__,
                )
            )

    return ArgContractOutcome(tool_name=tool_name, violations=tuple(violations))


_TOOL_ARG_CONTRACT_ENV = "HERMES_TOOL_ARG_CONTRACT"


def tool_arg_contract_enabled() -> bool:
    """Whether deterministic tool-argument contract enforcement is active.

    Default **ON** (issue #1530). The #1528 audit confirmed every native tool
    ships a safe schema (``required`` arrays + ``type`` declarations), and the
    check itself is fail-open on any schema without a structured contract
    (no ``parameters``/``properties``/``required``), so flipping the default ON
    only ever blocks calls that genuinely violate the schema the model was
    given — it cannot invent a stricter contract than the tool declared.

    Escape hatches (in priority order):

    * ``HERMES_TOOL_ARG_CONTRACT`` env var — ``0``/``false``/``no``/``off``
      disables; any truthy value forces ON. Read first so a session can flip
      the gate without editing config.
    * ``tool_arg_contract.enabled`` in ``config.yaml`` — set ``false`` to
      disable persistently.

    Any failure resolving config -> ON (the new safe default — the check is
    fail-open, so leaving it ON is the lower-risk choice). This differs from
    the historical default-OFF convention (and from its sibling
    :func:`agent.verify_policy.verify_policy_enabled`, which stays OFF) because
    #1528/#1529/#1530 made the check cheap, safe, and schema-gated.
    """
    env = os.environ.get(_TOOL_ARG_CONTRACT_ENV)
    if env is not None:
        return env.strip().lower() in {"1", "true", "yes", "on"}
    try:
        from hermes_cli.config import load_config as _load_config

        cfg = _load_config() or {}
    except Exception:
        return True
    section = cfg.get("tool_arg_contract") if isinstance(cfg, dict) else None
    if isinstance(section, dict) and "enabled" in section:
        return bool(section.get("enabled"))
    return True


__all__ = [
    "ArgContractViolation",
    "ArgContractOutcome",
    "check_tool_args_contract",
    "tool_arg_contract_enabled",
]
