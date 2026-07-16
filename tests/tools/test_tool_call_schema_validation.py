"""Tests for pre-execution schema validation in the tool_call dispatch path (#1039)."""
from __future__ import annotations
import json, os, sys
_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)
from tools.tool_search import validate_tool_args


def _schema(properties: dict, required: list | None = None) -> dict:
    return {"name": "dummy", "description": "test tool",
            "parameters": {"type": "object", "properties": properties, "required": required or []}}


class TestValidateToolArgs:
    def test_valid_args(self):
        ok, err = validate_tool_args("search", {"q": "hello"}, _schema({"q": {"type": "string"}}, ["q"]))
        assert ok and err is None

    def test_missing_required(self):
        ok, err = validate_tool_args("search", {}, _schema({"q": {"type": "string"}}, ["q"]))
        assert not ok and "Missing required parameter 'q'" in err

    def test_wrong_type_string_for_int(self):
        ok, err = validate_tool_args("calc", {"n": "42"}, _schema({"n": {"type": "integer"}}, ["n"]))
        assert not ok and "expected integer" in err

    def test_wrong_type_int_for_string(self):
        ok, err = validate_tool_args("search", {"q": 123}, _schema({"q": {"type": "string"}}, ["q"]))
        assert not ok and "expected string" in err

    def test_bool_rejected_for_integer(self):
        ok, err = validate_tool_args("calc", {"n": True}, _schema({"n": {"type": "integer"}}, ["n"]))
        assert not ok and "expected integer" in err

    def test_bool_accepted_for_boolean(self):
        assert validate_tool_args("t", {"flag": True}, _schema({"flag": {"type": "boolean"}}, []))[0]

    def test_number_and_array_object_types(self):
        s = _schema({"x": {"type": "number"}}, ["x"])
        assert validate_tool_args("f", {"x": 1}, s)[0]
        assert validate_tool_args("f", {"x": 1.5}, s)[0]
        ok, err = validate_tool_args("b", {"items": "no"}, _schema({"items": {"type": "array"}}, ["items"]))
        assert not ok and "expected array" in err
        ok, err = validate_tool_args("c", {"opts": 42}, _schema({"opts": {"type": "object"}}, ["opts"]))
        assert not ok and "expected object" in err

    def test_optional_union_no_schema_and_non_dict(self):
        s = _schema({"q": {"type": "string"}, "l": {"type": "integer"}}, ["q"])
        assert validate_tool_args("s", {"q": "hi", "l": None}, s)[0]
        su = _schema({"v": {"type": ["string", "integer"]}}, ["v"])
        assert validate_tool_args("u", {"v": "text"}, su)[0]
        ok, err = validate_tool_args("u", {"v": True}, su)
        assert not ok and "expected string or integer" in err
        assert validate_tool_args("any", {"x": 1}, None)[0]
        ok, err = validate_tool_args("search", "not a dict", s)  # type: ignore[arg-type]
        assert not ok and "must be an object" in err


class TestModelToolsDispatch:
    def test_wrong_type_returns_error_json(self, monkeypatch):
        from model_tools import handle_function_call
        from tools import tool_search as ts
        from tools.registry import registry
        tn = "__test_dummy_for_schema_validation"
        schema = {"name": tn, "description": "test", "parameters": {
            "type": "object", "properties": {"n": {"type": "integer"}}, "required": ["n"]}}
        class _E:
            def __init__(self):
                self.name, self.schema, self.check_fn = tn, schema, (lambda *a, **k: True)
                self.toolset, self.emoji, self.max_result_size_chars = "test", "", None
        monkeypatch.setattr(registry, "get_entry", lambda n: _E() if n == tn else None)
        monkeypatch.setattr(registry, "get_schema", lambda n: schema if n == tn else None)
        monkeypatch.setattr(ts, "is_deferrable_tool_name", lambda n, config=None: n == tn)
        monkeypatch.setattr(ts, "scoped_deferrable_names", lambda td: frozenset({tn}))
        result = handle_function_call(
            function_name="tool_call",
            function_args={"name": tn, "arguments": json.dumps({"n": "not an int"})},
        )
        parsed = json.loads(result)
        assert "error" in parsed
        assert "Parameter 'n'" in parsed["error"]