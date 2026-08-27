"""Tests for scripts/evolution_harness_gate.py (#2615, parent #2525).

The rework brief (PR #2686 review) demanded a REAL integration point: the gate
must be invoked by something. These tests pin both halves — the gate/apply
functions AND the cron-pass contract the ``evolution-harness-gate`` no_agent
job relies on.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from evolution_harness_gate import (  # noqa: E402
    GATE_SIDECAR,
    apply_validated,
    main,
    run_cron_pass,
    run_gate,
)
from evolution_harness_sandbox import GateResult  # noqa: E402

SURFACE = {
    "retry_count": 10,
    "backoff": {"base_delay_sec": 1.0, "multiplier": 2.0, "max_delay_sec": 60.0},
    "guard_conditions": [],
}
PROPOSAL = {
    "surface": SURFACE,
    "changes": [{"field": "retry_count", "before": 10, "after": 3, "reason": "cap"}],
    "status": "proposed",
    "requires_human_review": True,
    "auto_apply": False,
}
WEAKNESSES = {
    "weaknesses": [
        {
            "kind": "retry_spiral",
            "tool": "terminal",
            "max_consecutive": 9,
            "occurrences": 4,
        }
    ]
}


def _green(_):
    return GateResult(True, 0, "ok")


def _red(_):
    return GateResult(False, 1, "1 failed")


@pytest.fixture
def fake_gate(monkeypatch):
    """Keep CLI/cron tests off the real (minutes-long) regression gate."""
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, stdout="ok", stderr=""),
    )


def test_run_gate_verdicts():
    assert run_gate(PROPOSAL, gate_runner=_green)["status"] == "validated"
    red = run_gate(PROPOSAL, gate_runner=_red)
    assert red["status"] == "rejected" and red["requires_human_review"] is True


def test_apply_validated_only_writes_green(tmp_path):
    out = tmp_path / "retry_policy.json"
    assert apply_validated(run_gate(PROPOSAL, gate_runner=_green), out) is True
    assert json.loads(out.read_text())["retry_count"] == 3
    assert apply_validated(run_gate(PROPOSAL, gate_runner=_red), out) is False
    assert apply_validated({"status": "invalid", "applied": None}, out) is False


def test_cli_manual_dry_run_and_apply(tmp_path, capsys, fake_gate):
    prop = tmp_path / "p.json"
    prop.write_text(json.dumps(PROPOSAL))
    rc = main(["prog", str(prop)])
    assert rc == 0 and json.loads(capsys.readouterr().out)["status"] == "validated"
    assert not list(tmp_path.glob("surface.json"))  # dry-run writes nothing
    rc = main(["prog", str(prop), "--apply", "--out", str(tmp_path / "s.json")])
    assert rc == 0 and json.loads((tmp_path / "s.json").read_text())["retry_count"] == 3


def test_cli_apply_requires_out(tmp_path, capsys, fake_gate):
    prop = tmp_path / "p.json"
    prop.write_text(json.dumps(PROPOSAL))
    assert main(["prog", str(prop), "--apply"]) == 3  # EXIT_APPLY_REFUSED
    assert "--out" in capsys.readouterr().out


# -- cron integration (the Slice-C fix: a real, scheduled call site) --------


def test_cron_pass_gates_and_reports():
    report = run_cron_pass(WEAKNESSES, gate_runner=_green, surface=SURFACE)
    assert report["mode"] == "cron-report-only" and report["auto_apply"] is False
    assert report["gated"] == 1 and report["validated"] == 1
    assert report["verdicts"][0]["applied"]["after"]["retry_count"] == 3


def test_cron_pass_skips_no_code_diff():
    report = run_cron_pass(
        {"weaknesses": [{"kind": "tool_failure", "tool": "web_search"}]},
        gate_runner=_green,
        surface=SURFACE,
    )
    assert report["gated"] == 0 and report["skipped_no_code_diff"] == 1


def test_cron_mode_writes_sidecar(tmp_path, monkeypatch, capsys, fake_gate):
    """Zero-arg (scheduled) mode: gate the miner sidecar, write the report,
    exit 0, never apply."""
    prof = tmp_path / "profile"
    prof.mkdir()
    monkeypatch.setenv("EVOLUTION_PROFILE_DIR", str(prof))
    (prof / "weaknesses-latest.json").write_text(json.dumps(WEAKNESSES))
    assert main(["prog"]) == 0
    report = json.loads((prof / GATE_SIDECAR).read_text())
    assert report["auto_apply"] is False and report["gated"] == 1
    assert "apply is manual" in capsys.readouterr().out


def test_cron_mode_silent_without_sidecar(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("EVOLUTION_PROFILE_DIR", str(tmp_path / "none"))
    assert main(["prog"]) == 0 and capsys.readouterr().out == ""


def test_cron_yaml_registers_the_gate():
    """The YAML must declare the no_agent job register_evolution_cron.py picks up."""
    import yaml

    repo = Path(__file__).resolve().parents[2]
    spec = yaml.safe_load((repo / "cron/evolution/harness-gate.yaml").read_text())
    assert spec["name"] == "evolution-harness-gate"
    assert spec["no_agent"] is True
    assert spec["script"] == "evolution_harness_gate.py"
    assert (repo / "scripts" / spec["script"]).is_file()


def test_run_gate_holdout_validator_accepts_generalizing():
    candidate_prop = dict(PROPOSAL)
    candidate_prop["candidate"] = {
        "kind": "retry_spiral",
        "tool": "terminal",
        "occurrences": 10,
        "sessions": 5,
    }
    holdout_batch = [
        {"kind": "retry_spiral", "tool": "terminal", "sessions": 4},
    ]
    res = run_gate(candidate_prop, gate_runner=_green, holdout_batch=holdout_batch, min_sessions=3)
    assert res["status"] == "validated"


def test_run_gate_holdout_validator_rejects_overfitting():
    candidate_prop = dict(PROPOSAL)
    candidate_prop["candidate"] = {
        "kind": "retry_spiral",
        "tool": "terminal",
        "occurrences": 1,
        "sessions": 1,
    }
    holdout_batch = [
        {"kind": "retry_spiral", "tool": "terminal", "sessions": 1},
    ]
    res = run_gate(candidate_prop, gate_runner=_green, holdout_batch=holdout_batch, min_sessions=3)
    assert res["status"] == "invalid"
    assert "harness validation rejected" in res["reason"]
    assert res["zero_fitness"] is True


def test_cron_pass_with_holdout_filters_rejected_proposals():
    holdout_batch = [
        {"kind": "retry_spiral", "tool": "terminal", "sessions": 1},
    ]
    # Weakness with low occurrences/sessions fails holdout threshold
    weaknesses = {
        "weaknesses": [
            {
                "kind": "retry_spiral",
                "tool": "terminal",
                "occurrences": 1,
                "sessions": 1,
                "candidate": {"kind": "retry_spiral", "tool": "terminal", "occurrences": 1, "sessions": 1},
            }
        ]
    }
    report = run_cron_pass(
        weaknesses,
        gate_runner=_green,
        surface=SURFACE,
        holdout_batch=holdout_batch,
        min_sessions=3,
    )
    assert report["validator_rejected"] == 1
    assert report["validated"] == 0

