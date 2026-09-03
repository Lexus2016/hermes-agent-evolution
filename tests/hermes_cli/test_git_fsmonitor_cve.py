"""Tests for GitSpawn / CVE-2026-71963 mitigation across internal git calls.

Untrusted repositories can embed malicious core.fsmonitor or core.hooksPath
configurations in .git/config. When tools or background runners invoke
read-only git probes (such as status, diff, rev-parse), git by default executes
the configured executable as a daemon/hook.

Hermes Agent neutralizes this by enforcing noninteractive git environments
with GIT_CONFIG_PARAMETERS="'core.fsmonitor=false' 'core.hooksPath=/dev/null'".
"""

import subprocess
import sys
from pathlib import Path

import pytest

from hermes_cli._subprocess_compat import (
    bounded_git_probe,
    noninteractive_git_env,
)
from tools.subagent_worktree import _run_git
from cli import _worktree_is_dirty, _git_repo_root


def _setup_malicious_repo(tmp_path: Path, payload_marker: Path) -> Path:
    """Create a git repo whose .git/config contains an fsmonitor exploit payload."""
    repo = tmp_path / "malicious_repo"
    repo.mkdir(parents=True, exist_ok=True)

    # Initialize standard git repo
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Attacker"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "attacker@example.com"], cwd=repo, check=True)

    (repo / "README.md").write_text("untrusted content\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "initial commit"], cwd=repo, check=True)

    # Create malicious script that writes to payload_marker if executed
    exploit_script = tmp_path / "exploit.py"
    script_body = (
        "import sys\n"
        "from pathlib import Path\n"
        f"Path({repr(str(payload_marker))}).write_text('PWNED')\n"
        "sys.exit(0)\n"
    )
    exploit_script.write_text(script_body, encoding="utf-8")

    # Inject core.fsmonitor and core.hooksPath into local .git/config
    cmd_str = f"{sys.executable} {exploit_script}"
    git_config = repo / ".git" / "config"
    with open(git_config, "a", encoding="utf-8") as f:
        f.write(f'\n[core]\n\tfsmonitor = "{cmd_str}"\n\thooksPath = "{tmp_path}"\n')

    return repo


def test_noninteractive_git_env_includes_security_overrides():
    """Verify noninteractive_git_env defines core.fsmonitor and core.hooksPath overrides."""
    env = noninteractive_git_env()
    params = env.get("GIT_CONFIG_PARAMETERS", "")
    assert "core.fsmonitor=false" in params
    assert "core.hooksPath=/dev/null" in params
    assert env.get("GIT_TERMINAL_PROMPT") == "0"
    assert env.get("GCM_INTERACTIVE") == "Never"


def test_bounded_git_probe_neutralizes_fsmonitor(tmp_path):
    """bounded_git_probe must NOT trigger core.fsmonitor payload in an untrusted repo."""
    marker = tmp_path / "marker_bounded_probe.txt"
    repo = _setup_malicious_repo(tmp_path, marker)

    assert not marker.exists()

    # Probe status
    output = bounded_git_probe(["git", "-C", str(repo), "status", "--porcelain"], timeout=5.0)
    assert marker.exists() is False, "Exploit payload executed via bounded_git_probe!"


def test_subagent_worktree_run_git_neutralizes_fsmonitor(tmp_path):
    """subagent_worktree._run_git must NOT trigger core.fsmonitor payload."""
    marker = tmp_path / "marker_subagent_git.txt"
    repo = _setup_malicious_repo(tmp_path, marker)

    assert not marker.exists()

    result = _run_git(["status", "--porcelain"], cwd=str(repo), timeout=5)
    assert result.returncode == 0
    assert marker.exists() is False, "Exploit payload executed via _run_git!"


def test_cli_worktree_probes_neutralize_fsmonitor(tmp_path, monkeypatch):
    """cli git helpers must NOT trigger core.fsmonitor payload."""
    marker = tmp_path / "marker_cli_probes.txt"
    repo = _setup_malicious_repo(tmp_path, marker)

    assert not marker.exists()

    # Test _worktree_is_dirty
    dirty = _worktree_is_dirty(str(repo), timeout=5)
    assert marker.exists() is False, "Exploit payload executed via _worktree_is_dirty!"

    # Test _git_repo_root inside untrusted repo
    monkeypatch.chdir(repo)
    root = _git_repo_root()
    assert root is not None
    assert marker.exists() is False, "Exploit payload executed via _git_repo_root!"
