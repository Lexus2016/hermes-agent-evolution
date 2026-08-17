"""Tests for the Slice-C gated apply-path (scripts/evolution_harness_gate.py)."""

import json
import sys
from pathlib import Path
from types import SimpleNamespace

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import evolution_harness_gate as gate  # noqa: E402
from evolution_harness_sandbox import INVALID, REJECTED, VALIDATED  # noqa: E402

PROPOSAL = {
    "surface": {"retry_count": 3, "backoff": {"base": 1.0}, "guard_conditions": []},
    "changes": [{"field": "retry_count", "before": 3, "after": 4}],
}

_GREEN = SimpleNamespace(passed=True, exit_code=0, output="regression ok")
_RED = SimpleNamespace(passed=False, exit_code=1, output="regression failed")


def test_run_gate_validated_applies_in_sandbox():
    verdict = gate.run_gate(PROPOSAL, gate_runner=lambda applied: _GREEN)
    assert verdict["status"] == VALIDATED
    assert verdict["applied"]["after"]["retry_count"] == 4
    assert verdict["requires_human_review"] is True
    assert verdict["auto_apply"] is False


def test_run_gate_rejected_on_red_gate():
    verdict = gate.run_gate(PROPOSAL, gate_runner=lambda applied: _RED)
    assert verdict["status"] == REJECTED
    assert verdict["gate"]["passed"] is False


def test_run_gate_invalid_malformed():
    verdict = gate.run_gate({"surface": {}}, gate_runner=lambda applied: _GREEN)
    assert verdict["status"] == INVALID


def test_apply_validated_writes_only_when_green(tmp_path):
    out = tmp_path / "surface.json"
    green = gate.run_gate(PROPOSAL, gate_runner=lambda applied: _GREEN)
    assert gate.apply_validated(green, out) is True
    written = json.loads(out.read_text(encoding="utf-8"))
    assert written["retry_count"] == 4

    red = gate.run_gate(PROPOSAL, gate_runner=lambda applied: _RED)
    out2 = tmp_path / "surface-red.json"
    assert gate.apply_validated(red, out2) is False
    assert not out2.exists()


def test_main_apply_writes_on_green(tmp_path, monkeypatch, capsys):
    proposal_file = tmp_path / "proposal.json"
    proposal_file.write_text(json.dumps(PROPOSAL), encoding="utf-8")
    out = tmp_path / "surface.json"
    verdict = gate.run_gate(PROPOSAL, gate_runner=lambda applied: _GREEN)
    monkeypatch.setattr(gate, "run_gate", lambda *a, **k: verdict)

    rc = gate.main([str(proposal_file), "--apply", "--out", str(out)])
    assert rc == gate.EXIT_VALIDATED
    assert out.exists()
    assert '"status": "applied"' in capsys.readouterr().out


def test_main_refuses_apply_without_out(tmp_path, monkeypatch):
    proposal_file = tmp_path / "proposal.json"
    proposal_file.write_text(json.dumps(PROPOSAL), encoding="utf-8")
    verdict = gate.run_gate(PROPOSAL, gate_runner=lambda applied: _GREEN)
    monkeypatch.setattr(gate, "run_gate", lambda *a, **k: verdict)

    rc = gate.main([str(proposal_file), "--apply"])
    assert rc == gate.EXIT_APPLY_REFUSED
