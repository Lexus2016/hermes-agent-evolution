"""Tests for scripts/evolution_harness_gate.py (#2615, parent #2525).

The rework brief (PR #2686 review) demanded a REAL integration point: the
gate must be invoked by something. These tests pin both halves —
the pure gate/apply functions AND the cron-pass contract the
``evolution-harness-gate`` no_agent job relies on.
"""

import json
import sys
from pathlib import Path

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


def _green(_applied):
    return GateResult(True, 0, "all green")


def _red(_applied):
    return GateResult(False, 1, "1 failed")


# -- gate + apply functions ------------------------------------------------


def test_run_gate_green_and_red():
    assert run_gate(PROPOSAL, gate_runner=_green)["status"] == "validated"
    red = run_gate(PROPOSAL, gate_runner=_red)
    assert red["status"] == "rejected" and red["requires_human_review"] is True


def test_apply_validated_only_writes_green(tmp_path):
    green = run_gate(PROPOSAL, gate_runner=_green)
    out = tmp_path / "retry_policy.json"
    assert apply_validated(green, out) is True
    assert json.loads(out.read_text())["retry_count"] == 3
    # red and invalid verdicts are refused outright
    assert apply_validated(run_gate(PROPOSAL, gate_runner=_red), out) is False
    assert apply_validated({"status": "invalid", "applied": None}, out) is False


def test_cli_manual_dry_run_and_apply(tmp_path, capsys, monkeypatch):
    prop = tmp_path / "p.json"
    prop.write_text(json.dumps(PROPOSAL))

    def fake_run(cmd, **kw):  # keep the CLI off the real regression gate
        import subprocess

        return subprocess.CompletedProcess(cmd, 0, stdout="ok", stderr="")

    monkeypatch.setattr("subprocess.run", fake_run)
    rc = main(["prog", str(prop)])
    out = capsys.readouterr().out
    assert rc == 0 and json.loads(out.splitlines()[0])["status"] == "validated"
    assert not (tmp_path / "surface.json").exists()

    rc = main(["prog", str(prop), "--apply", "--out", str(tmp_path / "s.json")])
    assert rc == 0 and json.loads((tmp_path / "s.json").read_text())["retry_count"] == 3


def test_cli_apply_without_out_refused(tmp_path, capsys, monkeypatch):
    prop = tmp_path / "p.json"
    prop.write_text(json.dumps(PROPOSAL))

    def fake_run(cmd, **kw):
        import subprocess

        return subprocess.CompletedProcess(cmd, 0, stdout="ok", stderr="")

    monkeypatch.setattr("subprocess.run", fake_run)
    assert main(["prog", str(prop), "--apply"]) == 3  # EXIT_APPLY_REFUSED
    assert "--out" in capsys.readouterr().out


# -- cron integration (the Slice-C fix: a real call site) ------------------


def test_run_cron_pass_gates_miner_weaknesses():
    """Weaknesses -> proposals -> gated verdicts, report-only."""
    payload = {
        "weaknesses": [
            {
                "kind": "retry_spiral",
                "tool": "terminal",
                "occurrences": 5,
                "max_consecutive": 7,
                "signature": "",
                "label": "spiral",
            },
        ]
    }
    report = run_cron_pass(payload, gate_runner=_green, surface=SURFACE)
    assert report["mode"] == "cron-report-only"
    assert report["auto_apply"] is False
    assert report["gated"] == 1 and report["validated"] == 1
    assert report["verdicts"][0]["applied"]["after"]["retry_count"] == 3


def test_run_cron_pass_skips_proposals_without_code_diff():
    payload = {"weaknesses": [{"kind": "tool_failure", "tool": "web_search"}]}
    report = run_cron_pass(payload, gate_runner=_green, surface=SURFACE)
    assert report["gated"] == 0
    assert report["skipped_no_code_diff"] == 1


def test_cron_mode_zero_args_writes_sidecar(tmp_path, monkeypatch, capsys):
    """The scheduled job runs the script with NO args — it must read the
    miner sidecar, gate, write harness-gate-latest.json, exit 0, and NEVER
    apply anything."""
    prof = tmp_path / "profile"
    prof.mkdir()
    monkeypatch.setenv("EVOLUTION_PROFILE_DIR", str(prof))
    (prof / "weaknesses-latest.json").write_text(
        json.dumps({
            "weaknesses": [
                {
                    "kind": "retry_spiral",
                    "tool": "terminal",
                    "max_consecutive": 9,
                    "occurrences": 4,
                }
            ],
        })
    )

    def fake_run(cmd, **kw):
        import subprocess

        return subprocess.CompletedProcess(cmd, 0, stdout="ok", stderr="")

    monkeypatch.setattr("subprocess.run", fake_run)

    rc = main(["prog"])  # zero positional args -> cron mode
    assert rc == 0
    sidecar = prof / GATE_SIDECAR
    assert sidecar.is_file()
    report = json.loads(sidecar.read_text())
    assert report["auto_apply"] is False
    assert report["gated"] == 1
    assert "applied" in capsys.readouterr().out or report["validated"] == 1


def test_cron_mode_silent_when_no_sidecar(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("EVOLUTION_PROFILE_DIR", str(tmp_path / "none"))
    assert main(["prog"]) == 0
    assert capsys.readouterr().out == ""


def test_cron_yaml_registers_the_gate():
    """The integration the rework brief demanded: the YAML must declare the
    no_agent script job register_evolution_cron.py picks up."""
    import yaml

    repo = Path(__file__).resolve().parents[2]
    spec = yaml.safe_load(
        (repo / "cron" / "evolution" / "harness-gate.yaml").read_text()
    )
    assert spec["name"] == "evolution-harness-gate"
    assert spec["no_agent"] is True
    assert spec["script"] == "evolution_harness_gate.py"
    assert (repo / "scripts" / spec["script"]).is_file()
    # never silently self-modifying: the cron path cannot apply
    gate_src = (repo / "scripts" / "evolution_harness_gate.py").read_text()
    assert "--apply" in gate_src and "cron-report-only" in gate_src
