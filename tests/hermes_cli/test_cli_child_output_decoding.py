"""UTF-8 child diagnostics must not change CLI command outcomes."""

import json
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest


def _child_output(stdout, stderr, returncode):
    """Model text transport with caller-supplied decoding; never spawn a child."""
    def run(argv, **kwargs):
        result = subprocess.CompletedProcess(
            argv,
            returncode,
            stdout.decode(kwargs["encoding"], kwargs["errors"]),
            stderr.decode(kwargs["encoding"], kwargs["errors"]),
        )
        if kwargs.get("check"):
            result.check_returncode()
        return result

    return Mock(side_effect=run)


@pytest.mark.parametrize("returncode", [0, 128])
def test_commit_probe_keeps_sha_despite_non_ascii_diagnostics(monkeypatch, returncode):
    from hermes_cli import evolution_cmd

    run = _child_output(b"abc1234\n", "Сховище 日本語".encode() + b"\xff\n", returncode)
    monkeypatch.setattr(evolution_cmd.subprocess, "run", run)

    assert evolution_cmd._get_current_commit_sha() == ("abc1234" if returncode == 0 else None)
    run.assert_called_once_with(
        ["git", "rev-parse", "--short", "HEAD"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", check=True,
    )


@pytest.mark.parametrize("returncode", [0, 2])
def test_registrar_reports_non_ascii_output_without_changing_outcome(
    tmp_path, monkeypatch, capsys, returncode,
):
    from hermes_cli import main, profiles
    from tools.environments import local

    registrar = tmp_path / "scripts" / "register_evolution_cron.py"
    registrar.parent.mkdir()
    registrar.touch()
    (tmp_path / "cron" / "evolution").mkdir(parents=True)
    profile = SimpleNamespace(name="робота", path=tmp_path / "profile")
    jobs = profile.path / "cron" / "jobs.json"
    jobs.parent.mkdir(parents=True)
    jobs.write_text(json.dumps({"jobs": [{"name": "evolution-research"}]}), encoding="utf-8")
    env = {"HERMES_HOME": str(profile.path)}
    build_env = Mock(return_value=env)
    monkeypatch.setattr(main, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(profiles, "list_profiles", lambda: [profile])
    monkeypatch.setattr(local, "build_subprocess_env", build_env)
    run = _child_output(
        "Узгоджено 日本語\n".encode(), "Попередження 日本語 ".encode() + b"\xff\n", returncode,
    )
    monkeypatch.setattr(main.subprocess, "run", run)

    assert main._reconcile_existing_evolution_cron_jobs() == ([profile.name] if returncode == 0 else [])
    output = capsys.readouterr()
    assert "Узгоджено 日本語" in output.out
    assert output.err == "Попередження 日本語 \ufffd\n"
    assert ("Evolution cron reconcile failed" in output.out) is (returncode != 0)
    build_env.assert_called_once_with(
        inherit_profile_home=False, scrub_secrets=False, extra={"HERMES_HOME": str(profile.path)},
    )
    run.assert_called_once_with(
        [sys.executable, str(registrar)], cwd=tmp_path, env=env,
        capture_output=True, text=True, encoding="utf-8", errors="replace", check=False, timeout=300,
    )


@pytest.mark.parametrize("returncode", [0, 1])
def test_memory_probe_uses_returncode_despite_non_ascii_help(monkeypatch, returncode):
    from hermes_cli import tqmemory_setup

    run = _child_output(
        "Довідка 日本語 ".encode() + b"\xff\n", "Попередження".encode() + b"\xfe\n", returncode,
    )
    monkeypatch.setattr(tqmemory_setup.subprocess, "run", run)

    assert tqmemory_setup.verify_tqmemory("turbo-memory-mcp") is (returncode == 0)
    run.assert_called_once_with(
        ["turbo-memory-mcp", "--help"], check=False,
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=tqmemory_setup._VERIFY_TIMEOUT,
    )
