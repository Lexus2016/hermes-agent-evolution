"""Tests for gepa_validator held-out validation (issue #2232, Slice C)."""

from tools.gepa_evolution import EvolutionTree, run_gepa_generation
from tools.gepa_reflector import VariantResult
from tools.gepa_validator import HeldOutResult, promote_if_valid, validate_held_out


def _candidate():
    return EvolutionTree().add_seed("baseline text")


def _results(flags):
    return [
        VariantResult(variant="v", task=f"t{i}", passed=p) for i, p in enumerate(flags)
    ]


def test_validate_passed_when_above_threshold():
    res = validate_held_out(
        _candidate(), _results([True, False]), _results([True, True, False])
    )
    assert isinstance(res, HeldOutResult)
    assert res.passed is True
    assert res.n_passed == 2
    assert res.n_held_out == 3


def test_validate_failed_when_below_threshold():
    res = validate_held_out(
        _candidate(), _results([True]), _results([True, False, False, False])
    )
    assert res.passed is False
    assert res.pass_rate == 0.25


def test_validate_empty_held_out_fails():
    res = validate_held_out(_candidate(), _results([True]), [])
    assert res.passed is False
    assert res.n_held_out == 0


def test_promote_marks_selected_on_pass():
    cand = _candidate()
    res = validate_held_out(cand, _results([True]), _results([True, True]))
    assert promote_if_valid(cand, res) is True
    assert cand.selected is True
    assert cand.pruned is False
    assert "held_out_validation" in cand.metadata


def test_promote_marks_pruned_on_fail():
    cand = _candidate()
    res = validate_held_out(cand, _results([True]), _results([False, False]))
    assert promote_if_valid(cand, res) is False
    assert cand.selected is False
    assert cand.pruned is True


def test_held_out_gate_after_gepa_generation():
    """End-to-end: generate → validate → promote."""
    tree = EvolutionTree()
    seed = tree.add_seed("seed text")
    train = _results([True, False])
    child = run_gepa_generation(tree, seed, train)
    held_out = _results([True, True, True])
    res = validate_held_out(child, train, held_out, threshold=0.6)
    promote_if_valid(child, res)
    assert res.passed is True
    assert child.selected is True
    assert child.metadata["held_out_validation"]["n_held_out"] == 3


# ---------------------------------------------------------------------------
# Slice C persistence: promoted candidates survive to a JSONL ledger
# ---------------------------------------------------------------------------


def test_promoted_candidate_persisted_to_ledger(tmp_path, monkeypatch):
    """A PASSING candidate must be appended to the promotions ledger."""
    ledger = tmp_path / "promotions.jsonl"
    monkeypatch.setenv("GEPA_LEDGER_PATH", str(ledger))
    cand = _candidate()
    res = validate_held_out(cand, _results([True]), _results([True, True]))
    promote_if_valid(cand, res)
    assert ledger.exists()
    import json as _json

    lines = ledger.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    rec = _json.loads(lines[0])
    assert rec["candidate_id"] == cand.id
    assert rec["held_out_validation"]["n_passed"] == 2
    assert rec["promoted_at"]


def test_failed_candidate_not_persisted(tmp_path, monkeypatch):
    """A FAILING candidate must NOT be written to the ledger."""
    ledger = tmp_path / "promotions.jsonl"
    monkeypatch.setenv("GEPA_LEDGER_PATH", str(ledger))
    cand = _candidate()
    res = validate_held_out(cand, _results([True]), _results([False, False]))
    promote_if_valid(cand, res)
    assert not ledger.exists()
    assert cand.metadata["ledger_path"] if "ledger_path" in cand.metadata else True
