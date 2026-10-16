"""Tests for :mod:`tools.schema_validator` (issue #49).

Covers the structural JSON-Schema checks the Pydantic layer enforces:
valid schemas pass in every accepted shape (bare / function / wrapped),
and each common failure mode (bad type, required referencing an unknown
property, malformed enums, nested-object problems, non-dict input) yields
an actionable error string instead of a crash.
"""

from __future__ import annotations

import pytest

from tools.schema_validator import (
    ToolSchemaValidationResult,
    validate_tool_schema,
    validate_tool_schema_or_raise,
)


def _ok(schema) -> ToolSchemaValidationResult:
    return validate_tool_schema(schema)


class TestValidSchemas:
    """Every accepted shape must validate without modification."""

    def test_bare_parameters_schema(self):
        schema = {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "search text"},
                "limit": {"type": "integer", "minimum": 1},
            },
            "required": ["query"],
        }
        result = _ok(schema)
        assert result.ok is True
        assert result.errors == []
        assert result.normalized_parameters == schema

    def test_bare_function_schema_with_name_and_description(self):
        schema = {
            "name": "search_web",
            "description": "Search the web",
            "parameters": {
                "type": "object",
                "properties": {"q": {"type": "string"}},
                "required": ["q"],
            },
        }
        result = _ok(schema)
        assert result.ok is True
        # Normalization unwraps to the parameters dict.
        assert result.normalized_parameters is not None
        assert result.normalized_parameters["type"] == "object"
        assert "q" in result.normalized_parameters["properties"]

    def test_openai_wrapped_form(self):
        schema = {
            "type": "function",
            "function": {
                "name": "get_weather",
                "description": "Weather lookup",
                "parameters": {
                    "type": "object",
                    "properties": {"city": {"type": "string"}},
                    "required": ["city"],
                },
            },
        }
        result = _ok(schema)
        assert result.ok is True

    def test_empty_properties_and_no_required(self):
        assert _ok({"type": "object", "properties": {}}).ok is True
        assert (
            _ok({"type": "object", "properties": {"a": {"type": "string"}}}).ok is True
        )

    def test_unknown_extra_keywords_pass_through(self):
        """Provider-specific keywords must not break backward compatibility."""
        schema = {
            "type": "object",
            "properties": {
                "mode": {
                    "type": "string",
                    "enum": ["fast", "slow"],
                    "x-provider-hint": "ignored",
                    "default": "fast",
                    "deprecated": False,
                }
            },
            "additionalProperties": False,
            "x-root-extension": {"anything": "goes"},
        }
        result = _ok(schema)
        assert result.ok is True

    def test_nested_object_property(self):
        schema = {
            "type": "object",
            "properties": {
                "filters": {
                    "type": "object",
                    "properties": {
                        "tags": {"type": "array", "items": {"type": "string"}},
                        "strict": {"type": "boolean"},
                    },
                    "required": ["tags"],
                }
            },
            "required": ["filters"],
        }
        assert _ok(schema).ok is True

    def test_union_type_list(self):
        assert (
            _ok({
                "type": "object",
                "properties": {"v": {"type": ["string", "null"]}},
            }).ok
            is True
        )

    def test_array_property_without_items_still_passes(self):
        """Backward compat: some shipped schemas omit items; not a hard error."""
        assert (
            _ok({"type": "object", "properties": {"tags": {"type": "array"}}}).ok
            is True
        )


class TestInvalidSchemas:
    """Each structural failure must produce an actionable error string."""

    def test_non_dict_input(self):
        for bad in (None, "nope", 42, ["list"], object()):
            result = _ok(bad)
            assert result.ok is False
            assert result.errors, f"expected an error message for {bad!r}"
            assert result.normalized_parameters is None

    def test_dict_without_parameters_shape(self):
        result = _ok({"name": "no_parameters_here"})
        assert result.ok is False
        assert any("parameters" in e for e in result.errors)

    def test_invalid_root_type(self):
        result = _ok({"type": "string", "properties": {}})
        assert result.ok is False
        assert any("'object'" in e for e in result.errors)

    def test_unknown_json_schema_type(self):
        result = _ok({"type": "object", "properties": {"a": {"type": "banana"}}})
        assert result.ok is False
        assert any("banana" in e and "type" in e for e in result.errors)

    def test_required_references_unknown_property(self):
        result = _ok({
            "type": "object",
            "properties": {"known": {"type": "string"}},
            "required": ["ghost"],
        })
        assert result.ok is False
        assert any("'ghost'" in e for e in result.errors)
        assert any("required" in e for e in result.errors)

    def test_nested_required_references_unknown_property(self):
        """The recursive validator must catch problems inside nested objects."""
        result = _ok({
            "type": "object",
            "properties": {
                "inner": {
                    "type": "object",
                    "properties": {"a": {"type": "string"}},
                    "required": ["missing"],
                }
            },
        })
        assert result.ok is False
        assert any("missing" in e for e in result.errors)

    def test_enum_empty_list_rejected(self):
        result = _ok({
            "type": "object",
            "properties": {"m": {"type": "string", "enum": []}},
        })
        assert result.ok is False
        assert any("enum" in e for e in result.errors)

    def test_enum_items_must_be_valid(self):
        result = _ok({
            "type": "object",
            "properties": {"m": {"type": "string", "enum": ["a", "b"]}},
        })
        assert result.ok is True
        # A non-string enum on a string property is still structurally fine at
        # this layer (JSON Schema allows heterogeneous enums); the actionable
        # guarantee is that the enum keyword itself is a non-empty list.

    def test_required_must_be_strings(self):
        result = _ok({
            "type": "object",
            "properties": {"a": {"type": "string"}},
            "required": [1],
        })
        assert result.ok is False

    def test_never_raises_on_garbage(self):
        """The public API must return a result for any input, never throw."""
        weird_inputs = [
            {"type": "object", "properties": {"a": {"enum": [object()]}}},
            {"type": 7},
            {"properties": {"a": {"type": "string"}}, "required": {"not": "a list"}},
        ]
        for weird in weird_inputs:
            result = _ok(weird)
            assert isinstance(result, ToolSchemaValidationResult)


class TestRaiseVariant:
    def test_raises_with_joined_errors_on_invalid(self):
        with pytest.raises(ValueError, match="invalid tool schema"):
            validate_tool_schema_or_raise({
                "type": "object",
                "properties": {"a": {"type": 7}},
            })

    def test_returns_normalized_params_on_valid(self):
        params = validate_tool_schema_or_raise({
            "type": "object",
            "properties": {"q": {"type": "string"}},
            "required": ["q"],
        })
        assert params["type"] == "object"
        assert params["required"] == ["q"]
