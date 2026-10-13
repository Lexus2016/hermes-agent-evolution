"""Tests for sync_batch_probe (issue #3457). Minimal slice: probe core only."""

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


def _git_in(repo: str):
    original = subprocess.run

    def runner(cmd, *a, **kw):
        if isinstance(cmd, list) and cmd and cmd[0] == "git":
            kw["cwd"] = repo
        return original(cmd, *a, **kw)

    return runner


@pytest.fixture()
def syn_repo(tmp_path: Path) -> str:
    """Synthetic diverged repo: 3-way conflicts in agent/.github/tools/other."""
    repo = tmp_path

    def g(*a):
        subprocess.run(["git", *a], cwd=repo, check=True)

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
    for d in ("agent", "cron", ".github", "hermes_cli", "tools"):
        (repo / d).mkdir()
    for f in (
        "agent/a.py",
        "cron/b.py",
        ".github/c.yml",
        "hermes_cli/d.py",
        "tools/e.py",
        "README.md",
    ):
        (repo / f).write_text("base\n")
    ac("base")
    g("checkout", "-qb", "upstream")
    for p in ("agent/a.py", ".github/c.yml", "tools/e.py", "README.md"):
        (repo / p).write_text("upstream\n")
    (repo / "new_file.py").write_text("upstream\n")
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
    import sync_batch_probe as sbm

    monkeypatch.setattr(sbm.subprocess, "run", _git_in(syn_repo))
    probe = _probe_tag("main", "vTEST")
    assert probe.error is None, f"unexpected: {probe.error}"
    assert probe.total == 4
    assert probe.buckets["agent"] == 1
    assert probe.buckets[".github"] == 1
    assert probe.buckets["tools"] == 1
    assert probe.buckets["other"] == 1
    assert probe.buckets["cron"] == 0
    assert probe.buckets["hermes_cli"] == 0
    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=syn_repo,
        capture_output=True,
        text=True,
    )
    assert status.stdout.strip() == "", f"repo left dirty: {status.stdout!r}"
