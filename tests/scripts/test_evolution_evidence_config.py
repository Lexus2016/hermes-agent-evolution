"""Pinned evidence-gate thresholds (#3567, child of #3518).

The replay, evidence-span and evidence-type gates judge against thresholds that
live as data OUTSIDE the agent-writable tree. These tests prove a rewritten
in-tree copy cannot override the pinned surface, and that a missing or shadowed
surface fails closed.
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

import evolution_evidence_config as cfg
import evolution_replay_gate as replay


def _pin(tmp_path, monkeypatch, payload):
    pinned = tmp_path / "pinned" / "evolution_evidence_thresholds.json"
    pinned.parent.mkdir(exist_ok=True)
    pinned.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setenv("EVOLUTION_PINNED_EVIDENCE", str(pinned))
    return pinned


def _shipped_copy(**overrides):
    """The shipped thresholds with one section replaced — a hostile rewrite."""
    data = json.loads(cfg.seed_path().read_text(encoding="utf-8"))
    data.update(overrides)
    return data


def test_shipped_seed_loads_and_is_versioned():
    data = cfg.load_thresholds(cfg.seed_path())
    assert data["version"] >= 1
    assert set(data["replay"]["verdicts"]) == {
        "success",
        "failed",
        "incomplete",
        "empty",
    }
    assert data["span"]["max_span"] == 200
    assert "code_change" in data["type"]["expected_evidence"]


def test_rewritten_in_tree_copy_cannot_override_the_pin(tmp_path, monkeypatch):
    """The gate keeps the PINNED verdict map; the rewritten in-tree file loses."""
    pinned = _pin(
        tmp_path,
        monkeypatch,
        _shipped_copy(
            replay={
                "record_path": ".evolution/replay-record.json",
                "verdicts": {
                    "success": "pass",
                    "failed": "fail",
                    "incomplete": "incomplete",
                    "empty": "empty",
                },
                "certifiable": ["pass", "fail"],
                "marker": "pinned",
            }
        ),
    )
    rewritten = tmp_path / "tree" / "evolution_evidence_thresholds.json"
    rewritten.parent.mkdir()
    rewritten.write_text(
        json.dumps(
            _shipped_copy(
                replay={
                    "record_path": "evil/path",
                    "verdicts": {"success": "fail"},
                    "certifiable": ["fail"],
                    "marker": "rewritten",
                }
            )
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(cfg, "seed_path", lambda: rewritten)

    loaded = cfg.load_thresholds()
    assert loaded["source"] == str(pinned)
    assert loaded["replay"]["marker"] == "pinned"
    assert loaded["replay"]["record_path"] == ".evolution/replay-record.json"


def test_missing_pinned_surface_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setenv("EVOLUTION_PINNED_EVIDENCE", str(tmp_path / "absent.json"))
    with pytest.raises(ValueError, match="pinned evidence-gate surface missing"):
        cfg.load_thresholds()


def test_shadowed_pinned_surface_fails_closed(tmp_path, monkeypatch):
    pinned = _pin(tmp_path, monkeypatch, _shipped_copy())
    shadow = pinned.parent / "evolution_evidence_thresholds.in-tree.json"
    shadow.write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="shadowed"):
        cfg.load_thresholds()


def test_install_is_idempotent_and_refuses_divergence(tmp_path, monkeypatch):
    target = tmp_path / "deploy" / "evolution_evidence_thresholds.json"
    monkeypatch.setenv("EVOLUTION_PINNED_EVIDENCE", str(target))

    installed = cfg.install_pinned_thresholds()
    assert installed == target
    first = target.read_bytes()

    cfg.install_pinned_thresholds()  # identical bytes: no-op, no force
    assert target.read_bytes() == first

    target.write_text('{"version": 1}', encoding="utf-8")
    with pytest.raises(ValueError, match="differs from the in-tree seed"):
        cfg.install_pinned_thresholds()
    cfg.install_pinned_thresholds(force=True)
    assert target.read_bytes() == first


def test_replay_gate_judges_against_the_pinned_verdicts(tmp_path, monkeypatch):
    """A hostile pinned verdict map is exactly what the gate resolves."""
    hostile = json.loads(cfg.seed_path().read_text(encoding="utf-8"))
    hostile["replay"]["verdicts"] = {
        "success": "pass",
        "failed": "fail",
        "incomplete": "incomplete",
        "empty": "empty",
        "teleported": "pass",
    }
    _pin(tmp_path, monkeypatch, hostile)
    # The gate reads the PINNED map, not the in-tree seed.
    assert replay._replay()["verdicts"]["teleported"] == "pass"
    assert cfg.replay_config()["verdicts"] == hostile["replay"]["verdicts"]
