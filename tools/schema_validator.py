"""Type-safe tool schemas with Pydantic validation (issue #49).

Tool schemas in Hermes are defined as raw Python dicts — there is no
compile-time type checking or validation for tool input/output parameters.
Schema mismatches surface only at runtime as cryptic provider errors
(``400 format_error``, ``schema_validation_error``), wasting API calls and
agent retries.

This module is the standalone first slice of the type-safety layer: it wraps
tool parameter definitions in Pydantic ``BaseModel`` classes that enforce the
standard JSON Schema structure (``type``, ``properties``, ``required``) with
validators for common tool-schema patterns (string enums, optional fields,
nested objects, array constraints). It deliberately makes **no call-site
changes**: existing schemas must pass validation without modification
(backward compatibility is a success criterion of the issue).

Design notes
------------
* ``validate_tool_schema()`` is the single public entry point. It accepts the
  bare parameter schema, the bare function schema (``{"name": ...,
  "description": ..., "parameters": {...}}``), or the OpenAI-wrapped form
  (``{"type": "function", "function": {...}}``) and normalizes them all to
  the parameter dict before validation.
* Validation never raises on bad *input*: ``validate_tool_schema`` returns a
  :class:`ToolSchemaValidationResult` with human-actionable error strings.
  A Pydantic ``ValidationError`` from a malformed dict is translated into
  those strings, so callers have one error shape to handle.
* Unknown extra keywords pass through (``extra="allow"``): tool schemas carry
  a lot of provider- and model-specific keywords (``x-*`` extensions,
  ``default``, ``examples``, ``deprecated``...). Rejecting them would break
  backward compatibility for no safety gain.

The next planned slices (per the issue's split comment) consume this module:
a ``hermes tools validate`` CLI and Forge integration that runs generated
schemas through the validator before deployment.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

__all__ = [
    "JSON_SCHEMA_TYPES",
    "PropertySchema",
    "ToolSchema",
    "ToolSchemaValidationResult",
    "validate_tool_schema",
    "validate_tool_schema_or_raise",
]

#: The standard JSON Schema primitive types. ``integer`` and ``number`` are
#: distinct per JSON Schema (and per OpenAI tool schemas).
JSON_SCHEMA_TYPES = frozenset({
    "null",
    "boolean",
    "object",
    "array",
    "number",
    "string",
    "integer",
})

#: Keywords we validate structurally; everything else passes through.
_VALIDATED_JSON_SCHEMA_KEYWORDS = frozenset({
    "type",
    "properties",
    "required",
    "items",
    "enum",
})

#: Maximum nesting depth guard — a pathological schema must not blow the
#: Python stack via recursive Pydantic validation. 64 levels is far beyond
#: anything a real tool schema uses while staying safely under any limit.
_MAX_SCHEMA_DEPTH = 64


def _is_valid_schema_type(value: Any) -> bool:
    """True when *value* is a JSON Schema type or a list of valid types."""
    if isinstance(value, str):
        return value in JSON_SCHEMA_TYPES
    if isinstance(value, list) and value:
        return all(
            isinstance(item, str) and item in JSON_SCHEMA_TYPES for item in value
        )
    return False


class PropertySchema(BaseModel):
    """One JSON Schema property (recursive).

    ``extra="allow"`` preserves provider-specific keywords; only the
    structural keywords (``type``, ``properties``, ``required``, ``items``,
    ``enum``) are validated.
    """

    model_config = ConfigDict(extra="allow")

    type: Optional[str | list[str]] = None
    properties: Optional[dict[str, PropertySchema]] = None
    required: Optional[list[str]] = None
    items: Optional[PropertySchema] = None
    enum: Optional[list[Any]] = None

    @field_validator("type")
    @classmethod
    def _check_type(
        cls, value: Optional[str | list[str]]
    ) -> Optional[str | list[str]]:
        if value is None:
            return value
        if not _is_valid_schema_type(value):
            raise ValueError(
                f"type {value!r} is not a valid JSON Schema type "
                f"(expected one of {sorted(JSON_SCHEMA_TYPES)})"
            )
        return value

    @field_validator("required")
    @classmethod
    def _check_required(
        cls,
        value: Optional[list[str]],
        info: Any,
    ) -> Optional[list[str]]:
        if value is None:
            return value
        if not isinstance(value, list):
            raise ValueError("required must be a list of property names")
        properties = info.data.get("properties") or {}
        unknown = [name for name in value if name not in properties]
        if unknown:
            raise ValueError(
                "required lists properties that are not defined in 'properties': "
                + ", ".join(repr(n) for n in unknown)
            )
        return value

    @field_validator("enum")
    @classmethod
    def _check_enum(cls, value: Optional[list[Any]]) -> Optional[list[Any]]:
        if value is None:
            return value
        if not isinstance(value, list) or not value:
            raise ValueError("enum must be a non-empty list of allowed values")
        return value


class ToolSchema(BaseModel):
    """Root tool parameter schema.

    Enforces the structure the model providers expect for tool parameters:
    a JSON object whose properties are themselves valid JSON schemas, with
    ``required`` naming a subset of those properties.
    """

    model_config = ConfigDict(extra="allow")

    type: str | list[str] = "object"
    properties: dict[str, PropertySchema] = Field(default_factory=dict)
    required: Optional[list[str]] = None
    additionalProperties: Optional[bool] = None

    @field_validator("type")
    @classmethod
    def _check_root_type(cls, value: str | list[str]) -> str | list[str]:
        if not _is_valid_schema_type(value):
            raise ValueError(
                f"type {value!r} is not a valid JSON Schema type "
                f"(expected one of {sorted(JSON_SCHEMA_TYPES)})"
            )
        # Tool parameters must describe an object: providers bind tool args to
        # a JSON object of named properties. Anything else is a schema error.
        if isinstance(value, str) and value != "object":
            raise ValueError(
                f"tool parameter schema type must be 'object', got {value!r} "
                "(provider tool arguments are JSON objects)"
            )
        if isinstance(value, list) and "object" not in value:
            raise ValueError(
                f"tool parameter schema type list must include 'object', got {value!r}"
            )
        return value

    @field_validator("required")
    @classmethod
    def _check_required(
        cls,
        value: Optional[list[str]],
        info: Any,
    ) -> Optional[list[str]]:
        if value is None:
            return value
        if not isinstance(value, list):
            raise ValueError("required must be a list of property names")
        properties = info.data.get("properties") or {}
        unknown = [name for name in value if name not in properties]
        if unknown:
            raise ValueError(
                "required lists properties that are not defined in 'properties': "
                + ", ".join(repr(n) for n in unknown)
            )
        return value


# Resolve the forward references used for recursive property nesting.
PropertySchema.model_rebuild()
ToolSchema.model_rebuild()


@dataclass
class ToolSchemaValidationResult:
    """Outcome of :func:`validate_tool_schema`.

    ``errors`` is always a list of actionable, human-readable strings — one
    per structural problem found (empty means the schema is valid).
    """

    ok: bool
    errors: list[str] = field(default_factory=list)
    #: The schema normalized to the bare parameter dict, or None when the
    #: input was not a dict-shaped tool schema at all.
    normalized_parameters: Optional[dict[str, Any]] = None


def _extract_parameters(schema: Any) -> Optional[dict[str, Any]]:
    """Normalize any accepted tool-schema shape to the parameter dict.

    Accepts (mirroring :func:`agent.memory_manager.normalize_tool_schema`'s
    unwrap logic):

    * bare parameter schema  ``{"type": "object", "properties": ...}``
    * bare function schema   ``{"name": ..., "parameters": {...}}``
    * OpenAI-wrapped form    ``{"type": "function", "function": {...}}``

    Returns None when the input is not a dict or has no resolvable parameters.
    """
    if not isinstance(schema, dict):
        return None
    # Unwrap an already-wrapped OpenAI tool entry.
    if schema.get("type") == "function" and isinstance(schema.get("function"), dict):
        schema = schema["function"]
        if not isinstance(schema, dict):
            return None
    params = schema.get("parameters")
    if params is not None:
        if not isinstance(params, dict):
            return None
        return params
    # No "parameters" key: the dict is either a bare parameter schema
    # (identifiable by the JSON-Schema structural keys) or a function schema
    # that forgot its parameters — the latter must NOT be mistaken for a
    # bare parameter schema (a dict with only name/description would pass
    # structural validation and silently lose its parameters).
    if "type" in schema or "properties" in schema:
        return schema
    return None


def validate_tool_schema(schema: Any) -> ToolSchemaValidationResult:
    """Validate *schema* and return a :class:`ToolSchemaValidationResult`.

    Never raises for malformed input — structural problems are collected into
    ``errors``. The only exception is a genuine programmer error (e.g. a
    non-JSON-serializable object that breaks Pydantic coercion), which is
    also caught and reported as an error string so callers always get a
    result object.
    """
    params = _extract_parameters(schema)
    if params is None:
        return ToolSchemaValidationResult(
            ok=False,
            errors=[
                "tool schema must be a dict with a 'parameters' object (bare, function, or OpenAI-wrapped form)"
            ],
        )
    try:
        ToolSchema.model_validate(params)
    except ValidationError as exc:
        errors = _flatten_validation_errors(exc)
        return ToolSchemaValidationResult(
            ok=False, errors=errors, normalized_parameters=params
        )
    except Exception as exc:
        return ToolSchemaValidationResult(
            ok=False,
            errors=[
                f"unexpected schema validation failure: {type(exc).__name__}: {exc}"
            ],
            normalized_parameters=params,
        )
    return ToolSchemaValidationResult(ok=True, errors=[], normalized_parameters=params)


def validate_tool_schema_or_raise(schema: Any) -> dict[str, Any]:
    """Validate *schema* and return the normalized parameter dict, or raise.

    Useful for registration-time call sites that want fail-fast semantics
    (a bad schema must never reach the provider). Raises ``ValueError`` with
    all collected errors joined; never raises on structurally valid input.
    """
    result = validate_tool_schema(schema)
    if not result.ok:
        raise ValueError("invalid tool schema: " + "; ".join(result.errors))
    assert result.normalized_parameters is not None
    return result.normalized_parameters


def _flatten_validation_errors(exc: ValidationError) -> list[str]:
    """Turn a Pydantic ``ValidationError`` into flat, actionable strings.

    Each error is rendered as ``<location>: <message>`` so a caller can point
    a developer at the exact property (``properties.foo.required``) that is
    broken, which is the actionable shape the issue asks for.
    """
    flat: list[str] = []
    for err in exc.errors():
        loc = ".".join(str(part) for part in err.get("loc", ()))
        msg = str(err.get("msg", "invalid")).replace("\n", " ")
        flat.append(f"{loc}: {msg}" if loc else msg)
    return flat
