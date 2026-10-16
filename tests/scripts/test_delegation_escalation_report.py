"""The reporting CLI accepts plain UTF-8 and Windows BOM-prefixed inputs."""
import codecs
import json
from pathlib import Path
import subprocess
import sys

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts/delegation_escalation_report.py"


@pytest.mark.parametrize("trajectory_encoding", ["utf-8", "utf-8-sig"])
@pytest.mark.parametrize("outcome_encoding", ["utf-8", "utf-8-sig"])
@pytest.mark.parametrize("mode", ["summarize", "fit"])
def test_cli_reads_encoded_trajectories_and_verified_outcomes(
    tmp_path, trajectory_encoding, outcome_encoding, mode
):
    trajectories = tmp_path / "trajectories"
    trajectories.mkdir()
    (trajectories / "run.json").write_text(json.dumps({
        "session_id": "example",
        "entries": [
            {"tool": "delegation_contact", "args_summary": {
                "state": "start", "action": "CONSULT", "consultation_attempted": True,
            }},
            {"tool": "delegation_escalation_report", "args_summary": {
                "schema": "delegation-escalation-v1", "contacts": 1,
                "consultations": 1, "added_cost_usd": 0.25,
            }},
        ],
    }), encoding=trajectory_encoding)
    outcomes = tmp_path / "outcomes.json"
    outcomes.write_text(json.dumps({"example": {
        "verified": True, "reward": 1, "paired_baseline_verified": True,
        "baseline_evidence": "operator-reviewed baseline", "baseline_reward": 0,
    }}), encoding=outcome_encoding)
    output = tmp_path / "values.json"
    command = [sys.executable, str(SCRIPT), str(trajectories),
               "--verified-outcomes", str(outcomes)]
    if mode == "fit":
        command.extend(["--output", str(output)])
    result = subprocess.run(command, capture_output=True, text=True,
                            encoding="utf-8", timeout=30, check=False)
    assert result.returncode == 0, result.stderr
    if mode == "fit":
        assert not output.read_bytes().startswith(codecs.BOM_UTF8)
        fitted = json.loads(output.read_text(encoding="utf-8"))
        assert fitted["values"] == {"start": {"CONSULT": 1.0}}
        assert fitted["samples"] == {"start": {"CONSULT": 1}}
    else:
        summary = json.loads(result.stdout)
        assert summary["runs"] == 1
        assert summary["consultations"] == 1
        assert summary["added_cost_usd"] == 0.25
        assert summary["paired_runs"] == 1
        assert summary["recovered_failures_vs_always_proceed"] == 1
