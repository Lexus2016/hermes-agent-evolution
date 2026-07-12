"""Tests for scripts/evolution_analysis_gate.sh (#910).

evolution-analysis has two independent halves:

* Phase 0 — a LOCAL, file-based triage pass (``evolution_local_triage.py``)
  that reads issues/introspection/research sidecars and writes
  ``analysis/YYYY-MM-DD.json``. It needs no GitHub token or ``gh`` at all.
* Phase 1 — a GitHub-dependent full pass (``gh issue list``, triage/reject/
  close) that needs WRITE access to the repo.

Before this fix, evolution-analysis had no gate script of its own, so it
inherited the generic ``evolution_access_gate.sh`` default: a job-wide
wake-gate that skips the *entire* agent run — Phase 0 included — whenever
GitHub write access is unavailable. That meant the token-free local triage
pass never ran on days the private token was missing/scoped-down, even
though it needs no GitHub access whatsoever.

This suite asserts the fix's two guarantees independently:

1. ``analysis/YYYY-MM-DD.json`` (local triage output) is written regardless
   of GitHub write-access outcome — with push access, without push access,
   and with no ``gh``/token at all.
2. The wake-gate decision (last stdout line) still correctly reflects
   write access, exactly like ``evolution_access_gate.sh`` alone.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
GATE = REPO_ROOT / "scripts" / "evolution_analysis_gate.sh"
BASH = shutil.which("bash") or "/bin/bash"


def _write_fake_gh(bin_dir: Path, *, perms: str | None, authed: bool = True) -> None:
    """Install a fake `gh` that answers `api user` and `api repos/...`."""
    if perms is None:
        perms = "{}"
    user_branch = 'echo "tester"\n  exit 0' if authed else "exit 1"
    script = f"""#!/bin/bash
if [ "$1" = "api" ] && [ "$2" = "user" ]; then
  {user_branch}
fi
if [ "$1" = "api" ]; then
  case "$2" in
    repos/*) printf '%s' '{perms}'; exit 0 ;;
  esac
fi
exit 1
"""
    gh = bin_dir / "gh"
    gh.write_text(script)
    gh.chmod(gh.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _run_gate(tmp_path: Path, bin_dir: Path, gate: Path = GATE) -> tuple[str, Path]:
    """Run the gate; return (last stdout line, evolution dir).

    Unlike ``test_evolution_access_gate.py`` (which tests a script with no
    dependency beyond ``gh``/``curl``), this gate also runs
    ``evolution_local_triage.py`` via ``python3`` — so PATH here PREPENDS
    ``bin_dir`` (our fake ``gh``, or nothing) onto the real, inherited PATH
    rather than replacing it. That keeps the real ``python3`` (and whatever
    interpreter/shim mechanism it needs on the host) resolvable exactly as
    it would be in production, while still fully controlling which ``gh``
    the gate sees — the fake one in ``bin_dir`` always shadows any real one.

    ``gate`` defaults to the real ``scripts/evolution_analysis_gate.sh`` but
    can point at a copy in an isolated directory (e.g. to test behavior when
    a sibling script like ``evolution_access_gate.sh`` is deliberately absent).
    """
    home = tmp_path / "hermes_home"
    evolution_dir = home / "evolution"
    evolution_dir.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}"
    env["HERMES_HOME"] = str(home)
    env["GITHUB_EVOLUTION_REPO"] = "Owner/repo"
    for k in ("GITHUB_TOKEN", "GITHUB_PRIVATE_TOKEN"):
        env.pop(k, None)
    proc = subprocess.run(
        [BASH, str(gate)],
        capture_output=True,
        text=True,
        env=env,
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip().splitlines()[-1], evolution_dir


def _wakes(line: str) -> bool:
    return json.loads(line) == {"wakeAgent": True}


def _analysis_json(evolution_dir: Path) -> dict:
    analysis_dir = evolution_dir / "analysis"
    files = list(analysis_dir.glob("*.json"))
    assert len(files) == 1, f"expected exactly one analysis file, found {files}"
    return json.loads(files[0].read_text(encoding="utf-8"))


def test_push_access_wakes_and_writes_local_triage(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_fake_gh(
        bin_dir, perms='{"admin":false,"maintain":false,"push":true,"pull":true}'
    )
    line, evo_dir = _run_gate(tmp_path, bin_dir)
    assert _wakes(line)
    result = _analysis_json(evo_dir)
    assert result["local_triage"] is True


def test_read_only_account_does_not_wake_but_still_writes_local_triage(
    tmp_path: Path,
) -> None:
    """The exact bug this issue reports: reachable but push:false must still
    let Phase 0 (local triage) produce output — only the wake decision (Phase
    1, the LLM agent) should be gated on write access."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_fake_gh(
        bin_dir, perms='{"admin":false,"maintain":false,"push":false,"pull":true}'
    )
    line, evo_dir = _run_gate(tmp_path, bin_dir)
    assert not _wakes(line)
    result = _analysis_json(evo_dir)
    assert result["local_triage"] is True


def test_no_gh_no_token_does_not_wake_but_still_writes_local_triage(
    tmp_path: Path,
) -> None:
    """No gh on PATH and no env token: cannot confirm write access, so the
    agent must not wake — but local triage needs neither and must still run.
    This is the core regression this issue is about (#910)."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()  # deliberately empty: no gh, no curl
    line, evo_dir = _run_gate(tmp_path, bin_dir)
    assert not _wakes(line)
    result = _analysis_json(evo_dir)
    assert result["local_triage"] is True


def test_unauthenticated_does_not_wake_but_still_writes_local_triage(
    tmp_path: Path,
) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_fake_gh(bin_dir, perms="{}", authed=False)
    line, evo_dir = _run_gate(tmp_path, bin_dir)
    assert not _wakes(line)
    result = _analysis_json(evo_dir)
    assert result["local_triage"] is True


def test_missing_access_gate_fails_closed(tmp_path: Path) -> None:
    """If evolution_access_gate.sh is somehow absent (a degraded install —
    register_evolution_cron.py's _install_access_gate runs unconditionally,
    so this should never normally happen), we cannot confirm write access.
    The gate must fail CLOSED (not wake) rather than default to waking the
    agent — waking unconditionally would defeat the entire point of the
    write-access gate."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_scripts = tmp_path / "fake_scripts"
    fake_scripts.mkdir()
    shutil.copy(GATE, fake_scripts / GATE.name)
    shutil.copy(
        REPO_ROOT / "scripts" / "evolution_local_triage.py",
        fake_scripts / "evolution_local_triage.py",
    )
    # Deliberately do NOT copy evolution_access_gate.sh alongside it.
    line, evo_dir = _run_gate(tmp_path, bin_dir, gate=fake_scripts / GATE.name)
    assert not _wakes(line)
    result = _analysis_json(evo_dir)
    assert result["local_triage"] is True


def test_local_triage_reflects_real_sidecars(tmp_path: Path) -> None:
    """End-to-end: a filed issue in the issues/ sidecar surfaces in the
    gate-produced analysis JSON even when GitHub write access is denied."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_fake_gh(bin_dir, perms='{"push":false}')
    home = tmp_path / "hermes_home"
    issues_dir = home / "evolution" / "issues"
    issues_dir.mkdir(parents=True)
    (issues_dir / "2026-07-08.json").write_text(
        json.dumps({
            "date": "2026-07-08",
            "proposals": [
                {
                    "title": "Alpha",
                    "decision": "filed",
                    "issue": 100,
                    "priority_score": 1.5,
                    "impact": 0.8,
                    "effort": 0.3,
                }
            ],
        })
    )
    line, evo_dir = _run_gate(tmp_path, bin_dir)
    assert not _wakes(line)
    result = _analysis_json(evo_dir)
    nums = [s["issue_number"] for s in result["selected_for_implementation"]]
    assert 100 in nums
