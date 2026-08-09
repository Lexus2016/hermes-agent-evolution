"""Tests for falsifiable edit contracts (issue #1939)."""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "scripts"))
from evolution_edit_contracts import (  # noqa: E402
    contracts_summary,
    load_contracts,
    record_contract,
    validate_contract,
    verify_contracts,
)

_GOOD = {
    "failure_evidence": "test X fails on case Y",
    "root_cause": "missing null check",
    "targeted_fix": "add guard clause",
    "predicted_impact": {"should_flip": "case-Y", "cases": ["case-Y"]},
    "edit_type": "skill",
    "issue_number": 42,
}


def test_validate_good_contract():
    assert validate_contract(_GOOD) == []


def test_validate_missing_field():
    bad = dict(_GOOD, root_cause="")
    errors = validate_contract(bad)
    assert any("root_cause" in e for e in errors)


def test_validate_empty_predicted_impact():
    bad = dict(_GOOD, predicted_impact={})
    errors = validate_contract(bad)
    assert any("predicted_impact" in e for e in errors)


def test_record_contract_writes(tmp_path):
    result = record_contract(_GOOD, tmp_path)
    assert result["failure_evidence"] == "test X fails on case Y"
    assert result["auto_revert"] is True  # skill type is safe
    assert (tmp_path / "edit_contracts.jsonl").exists()
    lines = (tmp_path / "edit_contracts.jsonl").read_text().strip().split("\n")
    assert len(lines) == 1
    data = json.loads(lines[0])
    assert data["issue_number"] == 42
    assert data["verified"] is False


def test_record_contract_rejects_invalid(tmp_path):
    with pytest.raises(ValueError, match="missing"):
        record_contract({"failure_evidence": "x"}, tmp_path)


def test_record_auto_revert_for_safety_type():
    safety = dict(_GOOD, edit_type="harness")  # not in _SAFE_AUTO_REVERT
    result = record_contract(safety, Path("/tmp/test-ahe-dummy"))
    # Clean up
    p = Path("/tmp/test-ahe-dummy/edit_contracts.jsonl")
    if p.exists():
        p.unlink()
    assert result["auto_revert"] is False


def test_load_contracts_empty(tmp_path):
    assert load_contracts(tmp_path) == []


def test_load_contracts_multiple(tmp_path):
    record_contract(_GOOD, tmp_path)
    record_contract(dict(_GOOD, issue_number=43), tmp_path)
    contracts = load_contracts(tmp_path)
    assert len(contracts) == 2


def test_verify_confirmed(tmp_path):
    record_contract(_GOOD, tmp_path)
    results = verify_contracts({"case-Y": True}, tmp_path)
    assert results[0]["verified"] is True
    assert results[0]["verification_result"] == "confirmed"


def test_verify_missed(tmp_path):
    record_contract(_GOOD, tmp_path)
    results = verify_contracts({"case-Y": False}, tmp_path)
    assert results[0]["verification_result"] == "missed"


def test_verify_inconclusive(tmp_path):
    record_contract(_GOOD, tmp_path)
    results = verify_contracts({}, tmp_path)
    assert results[0]["verification_result"] == "inconclusive"


def test_verify_skips_already_verified(tmp_path):
    record_contract(_GOOD, tmp_path)
    verify_contracts({"case-Y": True}, tmp_path)
    results = verify_contracts({}, tmp_path)
    assert results[0]["verification_result"] == "confirmed"


def test_contracts_summary(tmp_path):
    record_contract(_GOOD, tmp_path)
    record_contract(dict(_GOOD, edit_type="memory", issue_number=43), tmp_path)
    s = contracts_summary(load_contracts(tmp_path))
    assert s["total"] == 2
    assert s["by_type"]["skill"] == 1
    assert s["by_type"]["memory"] == 1
    assert s["by_result"]["unverified"] == 2
