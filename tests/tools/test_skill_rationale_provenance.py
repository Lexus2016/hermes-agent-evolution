"""Tests for instruction provenance (rationale blocks) — issue #2629."""

import importlib

import pytest


@pytest.fixture
def usage_mod(tmp_path, monkeypatch):
    """Isolated HERMES_HOME + freshly-relocated skill_usage module."""
    home = tmp_path / ".hermes"
    (home / "skills").mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    mod = importlib.import_module("tools.skill_usage")
    importlib.reload(mod)
    monkeypatch.setattr(mod, "_prune_builtins_enabled", lambda: False)
    return mod


def test_empty_record_rationale_fields(usage_mod):
    rec = usage_mod._empty_record()
    assert rec["rationale"] is None
    assert rec["rationale_generation"] == 0


def test_rationale_decayed_missing_or_advanced(usage_mod):
    # Missing rationale => decayed by definition.
    assert usage_mod.rationale_decayed({}) is True
    assert (
        usage_mod.rationale_decayed({"patch_generation": 3, "rationale": None}) is True
    )
    # Rationale captured at gen 2, skill now at gen 3 => decayed.
    assert (
        usage_mod.rationale_decayed({
            "patch_generation": 3,
            "rationale_generation": 2,
            "rationale": {"why": "x"},
        })
        is True
    )
    # Up to date => not decayed.
    assert (
        usage_mod.rationale_decayed({
            "patch_generation": 3,
            "rationale_generation": 3,
            "rationale": {"why": "x"},
        })
        is False
    )
    # Custom gap threshold.
    assert (
        usage_mod.rationale_decayed(
            {
                "patch_generation": 4,
                "rationale_generation": 3,
                "rationale": {"why": "x"},
            },
            generations_gap=2,
        )
        is False
    )


def test_set_rationale_persists_and_records_generation(usage_mod):
    rec = {"patch_generation": 2}
    usage_mod._mutate("demo", lambda r: r.update(rec))
    usage_mod.set_rationale(
        "demo",
        "freeform why text",
        failure="cmd failed on fresh env",
        hypothesis="needs explicit PATH",
        outcome="solved",
    )
    stored = usage_mod.get_record("demo")
    assert stored["rationale"]["failure"] == "cmd failed on fresh env"
    assert stored["rationale"]["hypothesis"] == "needs explicit PATH"
    assert stored["rationale"]["outcome"] == "solved"
    assert stored["rationale"]["why"] == "freeform why text"
    assert stored["rationale_generation"] == 2
    assert usage_mod.rationale_decayed(stored) is False
