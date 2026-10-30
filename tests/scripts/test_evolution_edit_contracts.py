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


# --- issue #3554: a missed contract puts the previous note back, verbatim ---

_NOTES = "a note the editor replaced\n"


def _note(tmp_path):
    root = tmp_path / "skills"
    note = root / "evolution-analysis" / "SKILL.md"
    note.parent.mkdir(parents=True, exist_ok=True)
    note.write_text("the edit the contract predicted\n", encoding="utf-8")
    return root, note


def test_restore_refuses_outside_notes_root(tmp_path):
    from evolution_edit_contracts import restore_missed_contracts

    root, _ = _note(tmp_path)
    outside = tmp_path / "elsewhere" / "SKILL.md"
    outside.parent.mkdir(parents=True, exist_ok=True)
    outside.write_text("untouched\n", encoding="utf-8")
    record_contract(
        {
            **_G,
            "issue_number": 50,
            "prior_path": str(outside),
            "prior_content": _NOTES,
        },
        tmp_path,
    )
    verify_contracts({"cY": False}, tmp_path)
    acts = restore_missed_contracts(root, tmp_path, apply=True)
    assert acts[0]["restore_skipped"] == "outside_notes_root"
    assert outside.read_text(encoding="utf-8") == "untouched\n"


def test_restore_is_dry_run_by_default_and_verbatim_on_apply(tmp_path):
    from evolution_edit_contracts import restore_missed_contracts

    root, note = _note(tmp_path)
    record_contract(
        {**_G, "issue_number": 51, "prior_path": str(note), "prior_content": _NOTES},
        tmp_path,
    )
    verify_contracts({"cY": False}, tmp_path)

    preview = restore_missed_contracts(root, tmp_path)
    assert preview and preview[0]["applied"] is False
    assert note.read_text(encoding="utf-8") == "the edit the contract predicted\n"
    assert not load_contracts(tmp_path)[0].get("restored")

    applied = restore_missed_contracts(root, tmp_path, apply=True)
    assert applied[0]["applied"] is True
    assert note.read_text(encoding="utf-8") == _NOTES
    kept = load_contracts(tmp_path)[0]
    assert kept["restored"] and kept["restored_at"]
    # idempotent: a restored contract is not restored twice
    assert restore_missed_contracts(root, tmp_path, apply=True) == []


def test_no_restore_when_gain_landed_or_revert_not_allowed(tmp_path):
    from evolution_edit_contracts import restore_missed_contracts

    root, note = _note(tmp_path)
    record_contract(
        {**_G, "issue_number": 52, "prior_path": str(note), "prior_content": _NOTES},
        tmp_path,
    )
    verify_contracts({"cY": True}, tmp_path)  # gain landed -> keep the edit
    assert restore_missed_contracts(root, tmp_path, apply=True) == []
    assert note.read_text(encoding="utf-8") == "the edit the contract predicted\n"

    record_contract(
        {
            **_G,
            "edit_type": "harness",  # not a revertible note edit
            "issue_number": 53,
            "prior_path": str(note),
            "prior_content": _NOTES,
        },
        tmp_path,
    )
    results = verify_contracts({"cY": False}, tmp_path)
    assert results[-1]["verification_result"] == "missed"
    assert restore_missed_contracts(root, tmp_path, apply=True) == []


def test_capture_note_round_trips_bytes(tmp_path):
    from evolution_edit_contracts import capture_note

    root, note = _note(tmp_path)
    note.write_bytes("\u0456 body with a trailing space \n".encode("utf-8"))
    captured = capture_note(note)
    record_contract(
        {**_G, "issue_number": 54, "prior_path": str(note), "prior_content": captured},
        tmp_path,
    )
    verify_contracts({"cY": False}, tmp_path)
    note.write_text("replaced\n", encoding="utf-8")
    from evolution_edit_contracts import restore_missed_contracts

    restore_missed_contracts(root, tmp_path, apply=True)
    assert note.read_bytes() == captured.encode("utf-8")
