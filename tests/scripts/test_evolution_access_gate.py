"""Tests for scripts/evolution_access_gate.sh.

The wake-gate must wake the agent ONLY when the authenticated GitHub account has
WRITE (push|maintain|admin) access to the evolution repo — not merely when
GitHub is reachable. A read-only account passes `gh api user` but cannot push a
branch or open a PR, so waking it just burns LLM tokens.

We exercise the gate with a fake `gh` on PATH that returns a configurable
`permissions` object, and assert the final stdout line (the wake gate Hermes
cron reads).
"""

from __future__ import annotations

import json
import shutil
import stat
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
GATE = REPO_ROOT / "scripts" / "evolution_access_gate.sh"
# Resolve bash from the real environment; the gate is then run with an isolated
# PATH so only our fake `gh` (and no real gh/curl) is reachable from inside it.
BASH = shutil.which("bash") or "/bin/bash"


def _write_fake_gh(bin_dir: Path, *, perms: str | None, authed: bool = True) -> None:
    """Install a fake `gh` that answers `api user` and `api repos/...`.

    `gh api repos/<repo> --jq '.permissions // {}'` is emulated by echoing the
    already-jq-extracted permissions object (what real gh would print).
    """
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


def _run_gate(tmp_path: Path, bin_dir: Path) -> str:
    """Run the gate with an isolated PATH/HERMES_HOME; return the last stdout line."""
    home = tmp_path / "hermes_home"
    home.mkdir(exist_ok=True)
    env = {
        "PATH": str(bin_dir),  # only our fakes are reachable
        "HERMES_HOME": str(home),  # empty .env → no stray real token
        "GITHUB_EVOLUTION_REPO": "Owner/repo",
    }
    # Ensure no inherited tokens leak in.
    for k in ("GITHUB_TOKEN", "GITHUB_PRIVATE_TOKEN"):
        env.pop(k, None)
    proc = subprocess.run(
        [BASH, str(GATE)],
        capture_output=True,
        text=True,
        env=env,
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip().splitlines()[-1]


def _wakes(line: str) -> bool:
    return json.loads(line) == {"wakeAgent": True}


def test_push_access_wakes_agent(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_fake_gh(bin_dir, perms='{"admin":false,"maintain":false,"push":true,"pull":true}')
    assert _wakes(_run_gate(tmp_path, bin_dir))


def test_admin_access_wakes_agent(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_fake_gh(bin_dir, perms='{"admin":true,"maintain":true,"push":true,"pull":true}')
    assert _wakes(_run_gate(tmp_path, bin_dir))


def test_read_only_account_does_not_wake(tmp_path: Path) -> None:
    """The exact failure mode that prompted this fix: reachable but push:false."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_fake_gh(bin_dir, perms='{"admin":false,"maintain":false,"push":false,"pull":true}')
    assert not _wakes(_run_gate(tmp_path, bin_dir))


def test_spaced_json_push_true_wakes(tmp_path: Path) -> None:
    """Tolerate a pretty-printed permissions object (`"push": true`)."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_fake_gh(bin_dir, perms='{ "push": true, "pull": true }')
    assert _wakes(_run_gate(tmp_path, bin_dir))


def test_unauthenticated_does_not_wake(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_fake_gh(bin_dir, perms="{}", authed=False)
    assert not _wakes(_run_gate(tmp_path, bin_dir))


def test_no_gh_no_token_does_not_wake(tmp_path: Path) -> None:
    """No gh on PATH and no env token → cannot confirm write → skip."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()  # deliberately empty: no gh, no curl
    assert not _wakes(_run_gate(tmp_path, bin_dir))


def test_curl_fallback_keeps_token_off_argv(tmp_path: Path) -> None:
    """Security invariant: on the curl fallback path the token must reach curl
    via stdin (-H @-), never as a command-line argument (visible in `ps` /
    /proc/<pid>/cmdline). We install a fake `curl` that records its argv and
    stdin, run the gate with only a token in the env (no gh), and assert the
    token appears in stdin but NOT in argv.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()  # no gh → forces the curl fallback
    argv_log = tmp_path / "curl_argv.log"
    stdin_log = tmp_path / "curl_stdin.log"
    token = "SENTINEL_secret_pat_zzz"

    curl = bin_dir / "curl"
    curl.write_text(
        "#!/bin/bash\n"
        # Record argv (printf is a builtin — works with the isolated PATH).
        f'printf "%s\\n" "$@" > "{argv_log}"\n'
        # Capture ALL of stdin using a bash builtin (no external `cat`, which is
        # not on the isolated PATH). `read -d ""` reads until NUL == whole stdin.
        f'IFS= read -r -d "" _in\n'
        f'printf "%s" "$_in" > "{stdin_log}"\n'
        # Emulate GitHub returning a repo object with push access.
        "printf '%s' '{\"permissions\":{\"push\":true}}'\n"
        "exit 0\n"
    )
    curl.chmod(curl.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    home = tmp_path / "hermes_home"
    home.mkdir(exist_ok=True)
    env = {
        "PATH": str(bin_dir),
        "HERMES_HOME": str(home),
        "GITHUB_EVOLUTION_REPO": "Owner/repo",
        "GITHUB_PRIVATE_TOKEN": token,
    }
    proc = subprocess.run([BASH, str(GATE)], capture_output=True, text=True, env=env)
    assert proc.returncode == 0, proc.stderr

    # The gate still works (push:true → wake).
    assert _wakes(proc.stdout.strip().splitlines()[-1])
    argv = argv_log.read_text()
    # Sanity: the fake curl actually ran (non-vacuous assertion below).
    assert "@-" in argv, "fake curl did not run / -H @- missing"
    # The token must NOT be on the command line…
    assert token not in argv, "token leaked into curl argv (ps-visible)"
    # …but it MUST have been delivered via stdin.
    assert token in stdin_log.read_text()
