"""Tests for skill_postcondition_gate (issue #2255, Slice A)."""

import pytest

from tools.skill_postcondition_gate import (  # noqa: E402
    CaptureRecord,
    Postcondition,
    validate_capture,
)


def _file_exists(path):
    def check(state):
        return state.get("files", {}).get(path) is not None
    return check


def test_trusted_when_procedure_executed_and_all_postconditions_pass():
    record = CaptureRecord(
        skill_name="my-skill",
        procedure_executed=True,
        postconditions=[
            Postcondition("output file created", _file_exists("/tmp/out.txt"))
        ],
        post_execution_state={"files": {"/tmp/out.txt": "data"}},
    )
    assert validate_capture(record) is True
    assert record.trust_level == "trusted"
    assert record.validation_results[0]["passed"] is True


def test_rejected_when_procedure_not_executed():
    record = CaptureRecord(
        skill_name="my-skill",
        procedure_executed=False,
        postconditions=[
            Postcondition("output file created", _file_exists("/tmp/out.txt"))
        ],
        post_execution_state={"files": {}},
    )
    assert validate_capture(record) is False
    assert record.trust_level == "rejected"


def test_rejected_when_required_postcondition_fails():
    record = CaptureRecord(
        skill_name="my-skill",
        procedure_executed=True,
        postconditions=[
            Postcondition("output file created", _file_exists("/tmp/out.txt"))
        ],
        post_execution_state={"files": {}},
    )
    assert validate_capture(record) is False
    assert record.trust_level == "rejected"
    assert record.validation_results[0]["passed"] is False


def test_provisional_when_optional_postcondition_fails():
    record = CaptureRecord(
        skill_name="my-skill",
        procedure_executed=True,
        postconditions=[
            Postcondition(
                "optional cleanup ran",
                lambda state: state.get("cleaned", False),
                required=False,
            )
        ],
        post_execution_state={"cleaned": False},
    )
    assert validate_capture(record) is True
    assert record.trust_level == "provisional"


def test_skills_without_postconditions_are_rejected():
    # No postconditions -> nothing to verify -> rejected (not captured).
    record = CaptureRecord(
        skill_name="my-skill",
        procedure_executed=True,
        postconditions=[],
        post_execution_state={},
    )
    assert validate_capture(record) is False
    assert record.trust_level == "rejected"


def test_check_exception_treated_as_failure():
    def boom(state):
        raise RuntimeError("check crashed")

    record = CaptureRecord(
        skill_name="my-skill",
        procedure_executed=True,
        postconditions=[Postcondition("crashes", boom)],
        post_execution_state={},
    )
    assert validate_capture(record) is False
    assert record.trust_level == "rejected"


def test_to_metadata_serializes_trust_level():
    record = CaptureRecord(
        skill_name="my-skill",
        procedure_executed=True,
        postconditions=[
            Postcondition("output file created", _file_exists("/tmp/out.txt"))
        ],
        post_execution_state={"files": {"/tmp/out.txt": "data"}},
    )
    validate_capture(record)
    meta = record.to_metadata()
    assert meta["trust_level"] == "trusted"
    assert meta["procedure_executed"] is True
    assert meta["postconditions"][0]["description"] == "output file created"
