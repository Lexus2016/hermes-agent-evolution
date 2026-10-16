"""Tests for scripts/sync_batch_probe.py — probe core (issue #3457)."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "scripts"))
from sync_batch_probe import _bucket_for, _probe_tag, _markdown, TagProbe  # noqa: E402


def test_decisions_fail_closed_for_core_size_and_errors():
    assert _bucket_for(".github/workflows/x.yml") == ".github"
    assert _bucket_for("agent/foo.py") == "agent"
    assert _bucket_for("cron/jobs.py") == "cron"
    assert _bucket_for("hermes_cli/cli.py") == "hermes_cli"
    assert _bucket_for("tools/lint.py") == "tools"
    assert _bucket_for("README.md") == "other"

    assert TagProbe("small", total=10).verdict == "AUTONOMOUS"
    for probe in (
        TagProbe("large", total=11),
        TagProbe("core", total=1, core=1),
        TagProbe("bad", error="fatal"),
    ):
        assert probe.verdict == "OWNER-SESSION"
    assert "OWNER-SESSION" in _markdown([TagProbe("bad", error="fatal")])


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
        (repo / f).write_text("base\n", encoding="utf-8")
    ac("base")
    g("checkout", "-qb", "upstream")
    for p in ("agent/a.py", ".github/c.yml", "tools/e.py", "README.md"):
        (repo / p).write_text("upstream\n", encoding="utf-8")
    ac("upstream")
    g("tag", "vTEST")
    g("checkout", "-q", "main")
    for p in ("agent/a.py", ".github/c.yml", "tools/e.py", "README.md"):
        (repo / p).write_text("head\n", encoding="utf-8")
    for p in ("cron/b.py", "hermes_cli/d.py"):
        (repo / p).write_text("head-main-only\n", encoding="utf-8")
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
    assert probe.core == 1
    assert probe.verdict == "OWNER-SESSION"
    assert probe.paths == sorted([
        "agent/a.py",
        ".github/c.yml",
        "tools/e.py",
        "README.md",
    ])
    before = subprocess.run(
        ["git", "worktree", "list", "--porcelain"],
        cwd=syn_repo,
        capture_output=True,
        text=True,
        encoding="utf-8",
    ).stdout
    # Already-contained and clean non-fast-forward candidates both abort cleanly.
    assert _probe_tag("main", "main").total == 0
    subprocess.run(
        ["git", "checkout", "-qb", "clean", "main~1"], cwd=syn_repo, check=True
    )
    Path(syn_repo, "new.txt").write_text("new\n", encoding="utf-8")
    subprocess.run(["git", "add", "new.txt"], cwd=syn_repo, check=True)
    subprocess.run(
        ["git", "commit", "-qm", "clean candidate"], cwd=syn_repo, check=True
    )
    subprocess.run(["git", "checkout", "main"], cwd=syn_repo, check=True)
    clean = _probe_tag("main", "clean")
    assert clean.error is None and clean.total == 0
    subprocess.run(
        ["git", "checkout", "--orphan", "unrelated"], cwd=syn_repo, check=True
    )
    subprocess.run(["git", "commit", "-qm", "unrelated"], cwd=syn_repo, check=True)
    subprocess.run(["git", "checkout", "main"], cwd=syn_repo, check=True)
    failed = _probe_tag("main", "unrelated")
    assert failed.error and failed.verdict == "OWNER-SESSION"
    assert _probe_tag("main", "no-such-ref").error
    after = subprocess.run(
        ["git", "worktree", "list", "--porcelain"],
        cwd=syn_repo,
        capture_output=True,
        text=True,
        encoding="utf-8",
    ).stdout
    assert before == after
    cli = subprocess.run(
        [
            sys.executable,
            str(Path(__file__).resolve().parents[2] / "scripts/sync_batch_probe.py"),
            "--source",
            "main",
            "--tags",
            "vTEST",
            "clean",
            "unrelated",
        ],
        cwd=syn_repo,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert cli.returncode == 2, cli.stderr
    reports = json.loads(cli.stdout)
    assert [p["total"] for p in reports] == [probe.total, 0, 0]
    assert reports[-1]["error"] and reports[-1]["verdict"] == "OWNER-SESSION"
    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=syn_repo,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert status.stdout.strip() == "", f"repo left dirty: {status.stdout!r}"
