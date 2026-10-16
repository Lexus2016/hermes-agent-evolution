"""Standalone source scripts resolve Hermes YAML without injected PYTHONPATH."""

import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.parametrize("script", [
    "check_skills_spec.py", "evolution_disable_failed_mcp.py", "evolution_meta_editor.py",
    "evolution_rubric_judge.py", "evolution_skill_lint.py", "mcp_secret_audit.py",
    "register_evolution_cron.py",
])
def test_standalone_entrypoint_bootstraps_canonical_yaml(script, tmp_path):
    path = Path(__file__).resolve().parents[2] / "scripts" / script
    probe = (
        "import runpy, sys; "
        "runpy.run_path(sys.argv[1], run_name='entrypoint_test'); "
        "import hermes_yaml; "
        "assert hermes_yaml.safe_load('enabled: yes') == {'enabled': True}"
    )
    result = subprocess.run(
        [sys.executable, "-I", "-c", probe, str(path)], cwd=tmp_path,
        capture_output=True, text=True, encoding="utf-8", errors="replace", check=False,
    )
    assert result.returncode == 0, result.stderr
