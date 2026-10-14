"""Tests for scripts/sync_batch_probe.py — probe core (issue #3457)."""

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "scripts"))
from sync_batch_probe import _bucket_for, _probe_tag  # noqa: E402


def test_bucket_for_recognizes_known_buckets():
    assert _bucket_for(".github/workflows/x.yml") == ".github"
    assert _bucket_for("agent/foo.py") == "agent"
    assert _bucket_for("cron/jobs.py") == "cron"
    assert _bucket_for("hermes_cli/cli.py") == "hermes_cli"
    assert _bucket_for("tools/lint.py") == "tools"
    assert _bucket_for("README.md") == "other"


@pytest.fixture()
def syn_repo(tmp_path: Path) -> str:
    """Synthetic diverged repo: 3-way conflicts in agent/.github/tools/other."""
    repo = tmp_path

    def g(*a, check=True):
        subprocess.run(["git", *a], cwd=repo, check=check)

    def ac(msg):
        g("add", "-A")
        g("commit", "-qm", msg)

    g("init", "-q", "-b", "main")
    for k, v in (
        ("user.email", "t@t"),
        ("user.name", "t"),
        ("commit.gpgsign", "false"),
    ):
        g("config", k, v)
    for f in (
        ".github/c.yml",
        "agent/a.py",
        "cron/b.py",
        "hermes_cli/d.py",
        "tools/e.py",
        "README.md",
    ):
        (repo / f).parent.mkdir(parents=True, exist_ok=True)
        (repo / f).write_text("base\n")
    ac("base")
    g("checkout", "-qb", "upstream")
    for p in ("agent/a.py", ".github/c.yml", "tools/e.py", "README.md"):
        (repo / p).write_text("upstream\n")
    ac("upstream")
    g("tag", "vTEST")
    g("checkout", "-q", "main")
    for p in ("agent/a.py", ".github/c.yml", "tools/e.py", "README.md"):
        (repo / p).write_text("head\n")
    for p in ("cron/b.py", "hermes_cli/d.py"):
        (repo / p).write_text("head-main-only\n")
    ac("main edits")
    return str(repo)


def test_probe_tag_reports_known_conflicts(syn_repo, monkeypatch):
    # _probe_tag shells out with cwd="." → run the test from inside the repo
    monkeypatch.chdir(syn_repo)
    probe = _probe_tag("main", "vTEST")
    assert probe.error is None, f"unexpected: {probe.error}"
    assert probe.total == 4
    assert probe.buckets == {
        "agent": 1,
        ".github": 1,
        "tools": 1,
        "other": 1,
        "cron": 0,
        "hermes_cli": 0,
    }
    status = subprocess.run(
        ["git", "status", "--porcelain"], cwd=syn_repo, capture_output=True, text=True
    )
    assert status.stdout.strip() == "", f"repo left dirty: {status.stdout!r}"
