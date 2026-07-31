"""Tests for deterministic tool-argument contract enforcement (issue #904).

check_tool_args_contract() re-checks a tool call's arguments against the
tool's own registered JSON Schema (``required`` fields, ``enum`` values)
right before dispatch, so a call violating the schema the model was given
gets a clean, actionable error instead of either silently reaching a
handler that happens to tolerate it or raising deep inside the handler.
"""

import pytest

from agent.tool_arg_contract import (
    ArgContractOutcome,
    ArgContractViolation,
    check_tool_args_contract,
    tool_arg_contract_enabled,
)


# ── ArgContractViolation ─────────────────────────────────────────────────────


def test_missing_required_violation_shape():
    violation = ArgContractViolation.missing_required("path")
    assert violation.kind == "missing_required"
    assert violation.param == "path"
    assert "path" in violation.detail


def test_invalid_enum_violation_shape():
    violation = ArgContractViolation.invalid_enum("mode", "bogus", ("replace", "patch"))
    assert violation.kind == "invalid_enum"
    assert violation.param == "mode"
    assert "bogus" in violation.detail
    assert "replace" in violation.detail
    assert "patch" in violation.detail


# ── ArgContractOutcome ───────────────────────────────────────────────────────


def test_outcome_ok_when_no_violations():
    outcome = ArgContractOutcome(tool_name="read_file")
    assert outcome.ok is True


def test_outcome_not_ok_with_violations():
    outcome = ArgContractOutcome(
        tool_name="read_file",
        violations=(ArgContractViolation.missing_required("path"),),
    )
    assert outcome.ok is False


def test_outcome_error_message_covers_every_violation():
    outcome = ArgContractOutcome(
        tool_name="patch",
        violations=(
            ArgContractViolation.missing_required("path"),
            ArgContractViolation.invalid_enum("mode", "bogus", ("replace", "patch")),
        ),
    )
    msg = outcome.error_message()
    assert "patch" in msg
    assert "path" in msg
    assert "mode" in msg
    assert "bogus" in msg


# ── check_tool_args_contract: fail-open cases ───────────────────────────────


def test_no_schema_is_ok():
    outcome = check_tool_args_contract("mystery_tool", {}, None)
    assert outcome.ok is True


def test_schema_without_parameters_is_ok():
    outcome = check_tool_args_contract("mystery_tool", {}, {"name": "mystery_tool"})
    assert outcome.ok is True


def test_schema_without_properties_is_ok():
    schema = {"parameters": {"type": "object", "required": []}}
    outcome = check_tool_args_contract("mystery_tool", {}, schema)
    assert outcome.ok is True


def test_non_mapping_args_treated_as_empty():
    schema = {
        "parameters": {"properties": {"path": {"type": "string"}}, "required": ["path"]}
    }
    outcome = check_tool_args_contract("read_file", None, schema)
    assert outcome.ok is False
    assert outcome.violations[0].kind == "missing_required"


# ── required-field enforcement ──────────────────────────────────────────────


READ_FILE_SCHEMA = {
    "name": "read_file",
    "parameters": {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "offset": {"type": "integer", "default": 1},
        },
        "required": ["path"],
    },
}


def test_required_field_present_is_ok():
    outcome = check_tool_args_contract("read_file", {"path": "a.txt"}, READ_FILE_SCHEMA)
    assert outcome.ok is True


def test_required_field_missing_is_violation():
    outcome = check_tool_args_contract("read_file", {"offset": 5}, READ_FILE_SCHEMA)
    assert outcome.ok is False
    assert len(outcome.violations) == 1
    assert outcome.violations[0].kind == "missing_required"
    assert outcome.violations[0].param == "path"


def test_required_field_explicit_none_is_violation():
    """None means "no value supplied" for a non-nullable required field."""
    outcome = check_tool_args_contract("read_file", {"path": None}, READ_FILE_SCHEMA)
    assert outcome.ok is False
    assert outcome.violations[0].kind == "missing_required"


def test_required_nullable_field_explicit_none_is_ok():
    schema = {
        "parameters": {
            "properties": {"path": {"type": ["string", "null"]}},
            "required": ["path"],
        }
    }
    outcome = check_tool_args_contract("read_file", {"path": None}, schema)
    assert outcome.ok is True


def test_multiple_required_fields_all_missing_reported():
    schema = {
        "parameters": {
            "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
            "required": ["path", "content"],
        }
    }
    outcome = check_tool_args_contract("write_file", {}, schema)
    assert len(outcome.violations) == 2
    params = {v.param for v in outcome.violations}
    assert params == {"path", "content"}


def test_non_string_required_entries_are_skipped_defensively():
    schema = {
        "parameters": {
            "properties": {"path": {"type": "string"}},
            "required": ["path", 123, None],
        }
    }
    outcome = check_tool_args_contract("read_file", {"path": "a.txt"}, schema)
    assert outcome.ok is True


# ── enum enforcement ─────────────────────────────────────────────────────────


PATCH_SCHEMA = {
    "name": "patch",
    "parameters": {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "mode": {"type": "string", "enum": ["replace", "patch"]},
        },
        "required": ["path"],
    },
}


def test_enum_valid_value_is_ok():
    outcome = check_tool_args_contract(
        "patch", {"path": "a.txt", "mode": "replace"}, PATCH_SCHEMA
    )
    assert outcome.ok is True


def test_enum_invalid_value_is_violation():
    outcome = check_tool_args_contract(
        "patch", {"path": "a.txt", "mode": "bogus"}, PATCH_SCHEMA
    )
    assert outcome.ok is False
    assert outcome.violations[0].kind == "invalid_enum"
    assert outcome.violations[0].param == "mode"


def test_enum_field_omitted_is_ok():
    """An optional enum field the model didn't supply is not a violation."""
    outcome = check_tool_args_contract("patch", {"path": "a.txt"}, PATCH_SCHEMA)
    assert outcome.ok is True


def test_enum_field_none_is_ok():
    outcome = check_tool_args_contract(
        "patch", {"path": "a.txt", "mode": None}, PATCH_SCHEMA
    )
    assert outcome.ok is True


def test_missing_required_and_invalid_enum_both_reported():
    outcome = check_tool_args_contract("patch", {"mode": "bogus"}, PATCH_SCHEMA)
    kinds = {v.kind for v in outcome.violations}
    assert kinds == {"missing_required", "invalid_enum"}


# ── enable gate (default ON since #1530; env/config escape hatches) ──────────


def test_tool_arg_contract_enabled_by_default(monkeypatch):
    """#1530: the contract check is ON by default for every native tool whose
    schema was confirmed safe by the #1528 audit. A bare config (no
    tool_arg_contract section) and no env var → ON."""
    monkeypatch.delenv("HERMES_TOOL_ARG_CONTRACT", raising=False)
    monkeypatch.setattr(
        "hermes_cli.config.load_config", lambda *a, **k: {}, raising=False
    )
    assert tool_arg_contract_enabled() is True


@pytest.mark.parametrize("val", ["1", "true", "TRUE", "yes", "on"])
def test_tool_arg_contract_env_enables(monkeypatch, val):
    monkeypatch.setenv("HERMES_TOOL_ARG_CONTRACT", val)
    assert tool_arg_contract_enabled() is True


@pytest.mark.parametrize("val", ["0", "false", "no", "off", ""])
def test_tool_arg_contract_env_disables(monkeypatch, val):
    """The env var is the session-level escape hatch: ``0``/``false``/``no``/
    ``off`` forces the check OFF even though the default flipped to ON in
    #1530."""
    monkeypatch.setenv("HERMES_TOOL_ARG_CONTRACT", val)
    assert tool_arg_contract_enabled() is False


def test_tool_arg_contract_config_disables_when_env_absent(monkeypatch):
    """The config key is the persistent escape hatch: ``enabled: false`` in
    config.yaml disables the check even with no env var set."""
    monkeypatch.delenv("HERMES_TOOL_ARG_CONTRACT", raising=False)
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda *a, **k: {"tool_arg_contract": {"enabled": False}},
        raising=False,
    )
    assert tool_arg_contract_enabled() is False


def test_tool_arg_contract_config_enables_explicit(monkeypatch):
    """An explicit ``enabled: true`` config is honored and matches the new
    default (ON) — no behavior change, but documents the affirmative path."""
    monkeypatch.delenv("HERMES_TOOL_ARG_CONTRACT", raising=False)
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda *a, **k: {"tool_arg_contract": {"enabled": True}},
        raising=False,
    )
    assert tool_arg_contract_enabled() is True


def test_tool_arg_contract_config_load_error_defaults_on(monkeypatch):
    """#1530: if config can't be resolved (import error, corrupt yaml, etc.),
    the gate falls back to ON rather than OFF — the check is fail-open, so
    leaving it ON is the lower-risk choice."""
    monkeypatch.delenv("HERMES_TOOL_ARG_CONTRACT", raising=False)

    def _boom(*a, **k):
        raise RuntimeError("config load failed")

    monkeypatch.setattr("hermes_cli.config.load_config", _boom, raising=False)
    assert tool_arg_contract_enabled() is True


# ── type checking (issue #1529) ──────────────────────────────────────────────

TYPE_SCHEMA = {
    "name": "typed_tool",
    "parameters": {
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "count": {"type": "integer"},
            "ratio": {"type": "number"},
            "enabled": {"type": "boolean"},
            "tags": {"type": "array"},
            "config": {"type": "object"},
        },
        "required": ["name"],
    },
}


def test_type_mismatch_violation_shape():
    v = ArgContractViolation.type_mismatch("count", "x", "integer", "str")
    assert v.kind == "type_mismatch"
    assert v.param == "count"
    assert "integer" in v.detail and "str" in v.detail


def test_correct_types_are_ok():
    outcome = check_tool_args_contract(
        "typed_tool",
        {"name": "t", "count": 42, "ratio": 3.14, "enabled": True,
         "tags": ["a"], "config": {"k": "v"}},
        TYPE_SCHEMA,
    )
    assert outcome.ok is True


@pytest.mark.parametrize("param, bad_value", [
    ("name", 123), ("count", "x"), ("count", True), ("ratio", "x"),
    ("enabled", "yes"), ("tags", "x"), ("config", ["a"]),
])
def test_type_mismatch_detected(param, bad_value):
    outcome = check_tool_args_contract(
        "typed_tool", {"name": "ok", param: bad_value}, TYPE_SCHEMA
    )
    assert outcome.ok is False
    assert outcome.violations[0].kind == "type_mismatch"
    assert outcome.violations[0].param == param


def test_number_accepts_int():
    assert check_tool_args_contract("t", {"name": "ok", "ratio": 5}, TYPE_SCHEMA).ok is True


def test_array_accepts_tuple():
    assert check_tool_args_contract("t", {"name": "ok", "tags": ("a",)}, TYPE_SCHEMA).ok is True


def test_type_mismatch_union_type():
    schema = {"parameters": {"properties": {"v": {"type": ["string", "integer"]}}, "required": ["v"]}}
    assert check_tool_args_contract("t", {"v": 5}, schema).ok is True
    assert check_tool_args_contract("t", {"v": "hi"}, schema).ok is True
    assert check_tool_args_contract("t", {"v": 3.14}, schema).ok is False


def test_type_mismatch_unknown_type_is_ok():
    schema = {"parameters": {"properties": {"v": {"type": "custom"}}, "required": ["v"]}}
    assert check_tool_args_contract("t", {"v": "x"}, schema).ok is True


def test_no_type_declaration_skips_type_check():
    schema = {"parameters": {"properties": {"v": {"description": "no type"}}, "required": ["v"]}}
    assert check_tool_args_contract("t", {"v": 12345}, schema).ok is True


def test_none_value_skips_type_check():
    schema = {"parameters": {"properties": {"count": {"type": "integer"}}, "required": []}}
    assert check_tool_args_contract("t", {"count": None}, schema).ok is True


def test_type_mismatch_and_missing_required_both_reported():
    schema = {
        "parameters": {
            "properties": {"name": {"type": "string"}, "count": {"type": "integer"}},
            "required": ["name", "count"],
        }
    }
    outcome = check_tool_args_contract("t", {"count": "x"}, schema)
    assert {v.kind for v in outcome.violations} == {"missing_required", "type_mismatch"}


def test_type_mismatch_and_invalid_enum_both_reported():
    schema = {
        "parameters": {
            "properties": {
                "mode": {"type": "string", "enum": ["a", "b"]},
                "count": {"type": "integer"},
            },
            "required": [],
        }
    }
    outcome = check_tool_args_contract("t", {"mode": "c", "count": "x"}, schema)
    assert {v.kind for v in outcome.violations} == {"invalid_enum", "type_mismatch"}
