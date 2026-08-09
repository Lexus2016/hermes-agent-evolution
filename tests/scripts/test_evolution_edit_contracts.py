"""Tests for falsifiable edit contracts (issue #1939)."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "scripts"))
from evolution_edit_contracts import (  # noqa: E402
    contracts_summary,
    load_contracts,
    record_contract,
    verify_contracts,
)

_G = {
    "failure_evidence": "f",
    "root_cause": "n",
    "targeted_fix": "g",
    "predicted_impact": {"should_flip": "cY", "cases": ["cY"]},
    "edit_type": "skill",
    "issue_number": 42,
}


def test_contracts(tmp_path):
    with pytest.raises(ValueError, match="missing"):
        record_contract({"failure_evidence": "x"}, tmp_path)
    with pytest.raises(ValueError, match="predicted_impact"):
        record_contract({**_G, "predicted_impact": {}}, tmp_path)
    assert record_contract(_G, tmp_path)["auto_revert"] and not load_contracts(
        tmp_path / "x"
    )
    assert not record_contract({**_G, "edit_type": "harness"}, tmp_path)["auto_revert"]
    assert (
        verify_contracts({"cY": True}, tmp_path)[0]["verification_result"]
        == "confirmed"
    )
    record_contract({**_G, "issue_number": 43}, tmp_path)
    assert (
        verify_contracts({"cY": False}, tmp_path)[-1]["verification_result"] == "missed"
    )
    record_contract({**_G, "issue_number": 44}, tmp_path)
    assert verify_contracts({}, tmp_path)[-1]["verification_result"] == "inconclusive"
    s = contracts_summary(load_contracts(tmp_path))
    assert s["total"] == 4 and s["by_type"]["skill"] == 3
