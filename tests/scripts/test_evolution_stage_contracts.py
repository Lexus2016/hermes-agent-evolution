"""Pinned stage contracts (#3569): the edit stage and the skill stage read their
contract from a surface outside the checkout, refuse to run without it, and
follow the pinned copy when it changes."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(SCRIPTS))

from evolution_edit_contracts import record_contract, validate_contract, verify_contracts
from evolution_skill_contracts import check_repository_edits
from evolution_replay_gate import replay_grade
from evolution_stage_contracts import load_stage_contracts

pytestmark = pytest.mark.usefixtures("pinned_stage_contracts")

BASE = (
    "---\nname: example\ndescription: Example routing\ncontract:\n  version: 1\n"
    "  intent: Research\n  behavioral_criteria: [routing-applicability]\n"
    "  applies_to: [research]\n  excludes: [email]\n"
    "  protected_constraints: [Keep sources.]\n  replay_cases:\n"
    "    - {id: positive, task: research, applicable: true}\n---\nKeep sources.\n"
)
STRIPPED = "---\nname: example\ndescription: Example\n---\nKeep sources.\n"

CONTRACT = {
    "failure_evidence": "a gate missed a real regression",
    "root_cause": "the check read a stale baseline",
    "targeted_fix": "read the pinned baseline",
    "predicted_impact": {"cases": ["skill-replay"]},
}


def _git(root, *args):
    return subprocess.run(
        ["git", *args], cwd=root, check=True, capture_output=True, text=True, encoding="utf-8"
    ).stdout.strip()


def _repo(tmp_path):
    _git(tmp_path, "init")
    _git(tmp_path, "config", "user.email", "test@example.com")
    _git(tmp_path, "config", "user.name", "Test")
    target = tmp_path / "skills/example/SKILL.md"
    target.parent.mkdir(parents=True)
    target.write_text(BASE, encoding="utf-8")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-m", "baseline")
    return tmp_path


def test_missing_surface_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setenv("EVOLUTION_PINNED_STAGE_CONTRACTS", str(tmp_path / "absent.json"))
    with pytest.raises(ValueError, match="missing"):
        load_stage_contracts()
    with pytest.raises(ValueError, match="missing"):
        validate_contract(dict(CONTRACT))


def test_shadowed_surface_is_refused(pinned_stage_contracts):
    shadow = pinned_stage_contracts.parent / "evolution_stage_contracts.in-tree.json"
    shadow.write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="shadowed"):
        load_stage_contracts()


def test_pinned_required_fields_drive_edit_validation(pinned_stage_contracts):
    assert validate_contract(dict(CONTRACT)) == []
    data = json.loads(pinned_stage_contracts.read_text(encoding="utf-8"))
    data["edit_contract"]["required_fields"] = ["failure_evidence"]
    pinned_stage_contracts.write_text(json.dumps(data), encoding="utf-8")

    assert validate_contract({"failure_evidence": "x"}) == []
    errors = validate_contract({})
    assert errors[0] == "missing required field: failure_evidence"
    assert "root_cause" not in " ".join(errors)


def test_pinned_safe_revert_drives_the_recorded_contract(pinned_stage_contracts, tmp_path):
    assert record_contract(dict(CONTRACT), tmp_path / "evo")["auto_revert"] is True
    data = json.loads(pinned_stage_contracts.read_text(encoding="utf-8"))
    data["edit_contract"]["safe_revert_edit_types"] = ["memory"]
    pinned_stage_contracts.write_text(json.dumps(data), encoding="utf-8")

    assert record_contract(dict(CONTRACT), tmp_path / "evo")["auto_revert"] is False


def test_pinned_skill_roots_scope_the_replay(pinned_stage_contracts, tmp_path):
    root = _repo(tmp_path)
    target = root / "skills/example/SKILL.md"
    target.write_text(STRIPPED, encoding="utf-8")
    head = _git(root, "rev-parse", "HEAD")
    assert check_repository_edits(root, head)  # the seeded surface scans skills/

    data = json.loads(pinned_stage_contracts.read_text(encoding="utf-8"))
    data["skill_contract"]["scanned_roots"] = ["docs/"]
    pinned_stage_contracts.write_text(json.dumps(data), encoding="utf-8")

    assert check_repository_edits(root, head) == []


def test_skill_stage_fails_closed_without_surface(tmp_path, monkeypatch):
    monkeypatch.setenv("EVOLUTION_PINNED_STAGE_CONTRACTS", str(tmp_path / "absent.json"))
    errors = check_repository_edits(tmp_path, "HEAD")
    # The refusal names the stage-contract surface; a generic "baseline
    # unavailable" would also appear when the surface was quietly ignored.
    assert len(errors) == 1 and errors[0].startswith("Skill contract surface unavailable")


def test_pinned_baseline_reaches_the_production_callers(pinned_stage_contracts, tmp_path):
    root = _repo(tmp_path)
    baseline = _git(root, "rev-parse", "HEAD")
    (root / "skills/example/SKILL.md").write_text(
        BASE.replace("version: 1", "version: 2") + "More detail.\n", encoding="utf-8"
    )
    data = json.loads(pinned_stage_contracts.read_text(encoding="utf-8"))
    data["skill_contract"]["baseline"] = baseline
    pinned_stage_contracts.write_text(json.dumps(data), encoding="utf-8")

    # No explicit base: the replay must use the pinned baseline, not a
    # hardcoded ref left in the caller.
    assert check_repository_edits(root) == []
    assert replay_grade({"kind": "skill", "root": str(root)}) == ["pass"]


def test_pinned_required_fields_are_recorded(pinned_stage_contracts, tmp_path):
    data = json.loads(pinned_stage_contracts.read_text(encoding="utf-8"))
    data["edit_contract"]["required_fields"] = ["failure_evidence"]
    pinned_stage_contracts.write_text(json.dumps(data), encoding="utf-8")

    entry = record_contract({"failure_evidence": "x"}, tmp_path / "evo")

    assert entry["failure_evidence"] == "x"
    assert "root_cause" not in entry


def test_pinned_prediction_keys_are_verified(pinned_stage_contracts, tmp_path):
    data = json.loads(pinned_stage_contracts.read_text(encoding="utf-8"))
    data["edit_contract"]["predicted_impact_keys"] = ["regressions"]
    pinned_stage_contracts.write_text(json.dumps(data), encoding="utf-8")
    contract = dict(CONTRACT, predicted_impact={"regressions": ["case1"]})

    record_contract(contract, tmp_path / "evo")
    assert verify_contracts({"case1": True}, tmp_path / "evo")[0]["verification_result"] == "confirmed"
    record_contract(contract, tmp_path / "evo2")
    assert verify_contracts({"case1": False}, tmp_path / "evo2")[0]["verification_result"] == "missed"
