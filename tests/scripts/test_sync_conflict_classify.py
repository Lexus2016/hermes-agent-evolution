"""Tests for sync_conflict_classify (issue #3456)."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "scripts"))
from sync_conflict_classify import (  # noqa: E402
    BUCKETS,
    _bucket,
    _render_markdown,
    _summary,
    _verdict_for,
    classify,
    main,
)


# ── Pure-function unit tests ──────────────────────────────────────────────────


def test_verdict_upstream_wins_when_fork_equals_base():
    # Fork never changed the file → upstream-wins.
    assert _verdict_for(b"common", b"common", b"upstream-edited") == "upstream-wins"


def test_verdict_fork_keeps_when_upstream_equals_base():
    # Upstream never changed the file → fork-keeps.
    assert _verdict_for(b"common", b"fork-edited", b"common") == "fork-keeps"


def test_verdict_manual_when_both_changed():
    # Both sides changed → manual.
    assert _verdict_for(b"common", b"fork-edited", b"upstream-edited") == "manual"


def test_verdict_fork_keeps_when_fork_deleted_path_upstream_kept():
    # Fork deleted the file (None), upstream kept the same content as base.
    # base != fork (bytes vs None) → fork changed; base == upstream → upstream
    # did NOT change. Therefore fork-keeps (preserve the fork's deletion).
    assert _verdict_for(b"common", None, b"common") == "fork-keeps"


def test_verdict_manual_when_upstream_deleted_and_fork_diverged():
    # Upstream deleted the file (None) AND the fork changed the content vs
    # base. Both sides changed → manual, owner must look.
    assert _verdict_for(b"base", b"fork-new", None) == "manual"


def test_verdict_fork_keeps_when_fork_added_path_upstream_kept():
    # New file only on the fork (base=None, upstream kept nothing — also None).
    # base == upstream (both None) → upstream did NOT change; fork diverged.
    # Therefore fork-keeps (preserve the fork's new file).
    assert _verdict_for(None, b"new", None) == "fork-keeps"


def test_verdict_manual_when_all_three_differ():
    assert _verdict_for(b"base", b"fork-v2", b"upstream-v2") == "manual"


# ── Bucket helper ─────────────────────────────────────────────────────────────


def test_bucket_recognition():
    assert _bucket(".github/workflows/tests.yml") == ".github"
    assert _bucket("agent/foo.py") == "agent"
    assert _bucket("cron/jobs.py") == "cron"
    assert _bucket("hermes_cli/cli.py") == "hermes_cli"
    assert _bucket("tools/lint.py") == "tools"
    assert _bucket("README.md") == "other"
    assert _bucket("scripts/sync_conflict_classify.py") == "other"
    # Make sure every known bucket is covered.
    for b in BUCKETS:
        assert _bucket(f"{b}/anything") == b
        assert _bucket(b) == b


# ── Synthetic repo end-to-end (uses real git on tmp_path) ────────────────────


@pytest.fixture()
def syn_repo(tmp_path: Path) -> tuple[str, str, str]:
    """Build a tiny repo with three refs: base, fork, upstream.

    Files:
      agent/a.py       base=identical, fork=identical, upstream=edited
                       → upstream-wins
      cron/b.py        base=identical, fork=edited,    upstream=identical
                       → fork-keeps
      .github/c.yml    base=identical, fork=edited,    upstream=edited
                       → manual
      hermes_cli/d.py  base=identical, fork=identical, upstream=deleted
                       → upstream-wins
      tools/e.py       base=identical, fork=deleted,   upstream=identical
                       → fork-keeps (fork diverged from base)
      README.md        base=identical, fork=edited,    upstream=edited
                       → manual (bucket=other)
    """
    repo = tmp_path
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)

    # base
    (repo / "agent").mkdir()
    (repo / "cron").mkdir()
    (repo / ".github").mkdir()
    (repo / "hermes_cli").mkdir()
    (repo / "tools").mkdir()
    (repo / "agent" / "a.py").write_text("a=1\n")
    (repo / "cron" / "b.py").write_text("b=1\n")
    (repo / ".github" / "c.yml").write_text("name: c\n")
    (repo / "hermes_cli" / "d.py").write_text("d=1\n")
    (repo / "tools" / "e.py").write_text("e=1\n")
    (repo / "README.md").write_text("hi\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "base"], cwd=repo, check=True)
    subprocess.run(["git", "tag", "base"], cwd=repo, check=True)
    base = "base"

    # fork — edits cron/b.py, .github/c.yml, tools/e.py (delete), README.md
    subprocess.run(["git", "checkout", "-q", "-b", "fork"], cwd=repo, check=True)
    (repo / "cron" / "b.py").write_text("b=2  # fork edit\n")
    (repo / ".github" / "c.yml").write_text("name: c  # fork\n")
    (repo / "tools" / "e.py").unlink()
    (repo / "README.md").write_text("hi (fork)\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "fork"], cwd=repo, check=True)

    # upstream (separate branch off base) — edits agent/a.py, .github/c.yml,
    # deletes hermes_cli/d.py, edits README.md
    subprocess.run(
        ["git", "checkout", "-q", "-b", "upstream", base], cwd=repo, check=True
    )
    (repo / "agent" / "a.py").write_text("a=2  # upstream edit\n")
    (repo / ".github" / "c.yml").write_text("name: c  # upstream\n")
    (repo / "hermes_cli" / "d.py").unlink()
    (repo / "README.md").write_text("hi (upstream)\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "upstream"], cwd=repo, check=True)

    # We need HEAD accessible via "fork" ref — point it at the fork tip.
    subprocess.run(["git", "checkout", "-q", "fork"], cwd=repo, check=True)
    return (str(repo), base, "upstream")


def test_classify_end_to_end(syn_repo, monkeypatch, tmp_path, capsys):
    repo, base, upstream = syn_repo
    fork = "fork"
    # Run git from inside the synthetic repo.
    monkeypatch.setattr(subprocess, "run", _wrap_git_run(repo, subprocess.run))

    paths = [
        "agent/a.py",
        "cron/b.py",
        ".github/c.yml",
        "hermes_cli/d.py",
        "tools/e.py",
        "README.md",
    ]
    out_dir = tmp_path / "out"
    argv = [
        "sync_conflict_classify",
        "--base",
        base,
        "--fork",
        fork,
        "--upstream",
        upstream,
        "--tag",
        "vTEST",
        "--out-dir",
        str(out_dir),
    ]
    # Add paths via --path
    for p in paths:
        argv.extend(["--path", p])
    monkeypatch.setattr(sys, "argv", argv)

    rc = main()
    out = capsys.readouterr().out
    # Two manual entries (c.yml and README.md) → exit 1, expected.
    assert rc == 1, out
    assert "upstream-wins" in out and "fork-keeps" in out and "manual" in out

    j = json.loads((out_dir / "sync-conflicts-vTEST.json").read_text())
    by_v = {v["path"]: v["verdict"] for v in j["verdicts"]}
    assert by_v["agent/a.py"] == "upstream-wins"
    assert by_v["cron/b.py"] == "fork-keeps"
    assert by_v[".github/c.yml"] == "manual"
    assert by_v["hermes_cli/d.py"] == "upstream-wins"
    assert by_v["tools/e.py"] == "fork-keeps"
    assert by_v["README.md"] == "manual"

    # Bucket accounting
    summary = j["summary"]
    assert summary["by_verdict"]["upstream-wins"] == 2
    assert summary["by_verdict"]["fork-keeps"] == 2
    assert summary["by_verdict"]["manual"] == 2
    assert summary["by_bucket"]["agent"]["upstream-wins"] == 1
    assert summary["by_bucket"]["cron"]["fork-keeps"] == 1
    assert summary["by_bucket"][".github"]["manual"] == 1
    assert summary["by_bucket"]["hermes_cli"]["upstream-wins"] == 1
    assert summary["by_bucket"]["tools"]["fork-keeps"] == 1
    assert summary["by_bucket"]["other"]["manual"] == 1

    md = (out_dir / "sync-conflicts-vTEST.md").read_text()
    assert "Sync conflict classification — `vTEST`" in md
    assert "Total paths: **6**" in md
    assert "| bucket | upstream-wins | fork-keeps | manual | total |" in md


def test_summary_helper():
    from sync_conflict_classify import Verdict

    vs = [
        Verdict("agent/a.py", "upstream-wins", "agent", True, True, True),
        Verdict("cron/b.py", "fork-keeps", "cron", True, True, True),
        Verdict(".github/c.yml", "manual", ".github", True, True, True),
    ]
    s = _summary(vs)
    assert s["total"] == 3
    assert s["by_verdict"]["upstream-wins"] == 1
    assert s["by_verdict"]["fork-keeps"] == 1
    assert s["by_verdict"]["manual"] == 1
    # All BUCKETS keys present, even zero-count
    for b in (*BUCKETS, "other"):
        assert b in s["by_bucket"]


def test_render_markdown_shape():
    from sync_conflict_classify import Verdict

    vs = [Verdict("agent/a.py", "upstream-wins", "agent", True, True, True)]
    s = _summary(vs)
    md = _render_markdown("vTEST", s, vs)
    assert "# Sync conflict classification — `vTEST`" in md
    assert "Total paths: **1**" in md
    assert "| `agent/a.py` | upstream-wins | agent |" in md


# ── Test helper: redirect git invocations into the synthetic repo ────────────


def _wrap_git_run(repo: str, real_run):
    """Return a wrapper that runs `git` inside the synthetic repo."""
    import functools

    def runner(*args, **kwargs):
        cmd = args[0] if args else kwargs.get("args")
        if isinstance(cmd, list) and cmd and cmd[0] == "git":
            kwargs["cwd"] = repo
        return real_run(*args, **kwargs)

    return runner
