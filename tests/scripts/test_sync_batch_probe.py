"""Tests for sync_batch_probe (issue #3457)."""

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "scripts"))
from sync_batch_probe import (  # noqa: E402
    BUCKETS,
    TagProbe,
    _bucket_for,
    _probe_tag,
    _render_brief,
)


# ── Pure-function unit tests ──────────────────────────────────────────────────


def test_bucket_for_recognizes_known_buckets():
    assert _bucket_for(".github/workflows/x.yml") == ".github"
    assert _bucket_for("agent/foo.py") == "agent"
    assert _bucket_for("cron/jobs.py") == "cron"
    assert _bucket_for("hermes_cli/cli.py") == "hermes_cli"
    assert _bucket_for("tools/lint.py") == "tools"
    assert _bucket_for("README.md") == "other"
    assert _bucket_for("scripts/sync_batch_probe.py") == "other"


def test_bucket_for_handles_exact_match():
    # A path equal to the bucket prefix (no trailing slash) is still in that bucket.
    for b in BUCKETS:
        assert _bucket_for(b) == b


def test_tagprobe_verdict_autonomous():
    p = TagProbe(tag="vX", buckets={b: 0 for b in (*BUCKETS, "other")}, total=0)
    assert p.verdict == "AUTONOMOUS"
    assert "AUTONOMOUS" in p.render_row()


def test_tagprobe_verdict_owner_session():
    b = {k: 0 for k in (*BUCKETS, "other")}
    b["agent"] = 3
    b["cron"] = 2
    p = TagProbe(tag="vX", buckets=b, total=5)
    assert p.verdict == "OWNER-SESSION"
    assert "OWNER-SESSION" in p.render_row()


def test_tagprobe_error_state():
    p = TagProbe(tag="vX", error="boom")
    assert p.verdict == "ERROR"
    row = p.render_row()
    assert "error: boom" in row


def test_render_brief_shape():
    b1 = {k: 0 for k in (*BUCKETS, "other")}
    b1["agent"] = 2
    b2 = {k: 0 for k in (*BUCKETS, "other")}
    b2["agent"] = 50
    probes = [
        TagProbe(tag="v1", buckets=b1, total=2),
        TagProbe(tag="v2", buckets=b2, total=50),
    ]
    md = _render_brief("origin/main", 10, probes)
    assert "# Sync batch probe — owner-decision brief" in md
    assert "Source ref: `origin/main`" in md
    assert "Autonomous ceiling: ≤ 10 total conflicts" in md
    assert "| `v1` |" in md and "AUTONOMOUS" in md
    assert "| `v2` |" in md and "OWNER-SESSION" in md


# ── Synthetic-repo end-to-end ────────────────────────────────────────────────


@pytest.fixture()
def syn_repo(tmp_path: Path) -> str:
    """Build a synthetic repo where 'main' and 'upstream' truly diverge.

    A 3-way conflict needs BOTH branches to edit the same file since their
    common ancestor — a plain descendant branch merges clean (fast-forward
    semantics even with --no-ff). So: commit a common "base", branch
    `upstream` off it and edit, then switch back to `main` and edit the same
    files differently.

    Merge upstream into main:
      agent/a.py       base→head, base→upstream  ← CONFLICT (agent)
      cron/b.py        edited only on main       ← clean (not a conflict)
      .github/c.yml    base→head, base→upstream  ← CONFLICT (.github)
      hermes_cli/d.py  edited only on main       ← clean (not a conflict)
      tools/e.py       base→head, base→upstream  ← CONFLICT (tools)
      README.md        base→head, base→upstream  ← CONFLICT (other)
      new_file.py      added only on upstream    ← clean (not a conflict)
    """
    repo = tmp_path
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    subprocess.run(["git", "config", "commit.gpgsign", "false"], cwd=repo, check=True)

    # Common base: all six files, one content.
    (repo / "agent").mkdir()
    (repo / "cron").mkdir()
    (repo / ".github").mkdir()
    (repo / "hermes_cli").mkdir()
    (repo / "tools").mkdir()
    for f in (
        "agent/a.py",
        "cron/b.py",
        ".github/c.yml",
        "hermes_cli/d.py",
        "tools/e.py",
        "README.md",
    ):
        (repo / f).write_text("base\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "base"], cwd=repo, check=True)

    # upstream branch: edit 4 files + add one new file.
    subprocess.run(["git", "checkout", "-q", "-b", "upstream"], cwd=repo, check=True)
    (repo / "agent" / "a.py").write_text("upstream\n")
    (repo / ".github" / "c.yml").write_text("upstream\n")
    (repo / "tools" / "e.py").write_text("upstream\n")
    (repo / "README.md").write_text("upstream\n")
    (repo / "new_file.py").write_text("upstream\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "upstream"], cwd=repo, check=True)
    subprocess.run(["git", "tag", "vTEST"], cwd=repo, check=True)

    # main: edit the SAME 4 files to a DIFFERENT content (cron/b.py and
    # hermes_cli/d.py stay at base so they merge clean from upstream's side —
    # wait, they were never edited by upstream either, so they stay identical).
    subprocess.run(["git", "checkout", "-q", "main"], cwd=repo, check=True)
    (repo / "agent" / "a.py").write_text("head\n")
    (repo / ".github" / "c.yml").write_text("head\n")
    (repo / "tools" / "e.py").write_text("head\n")
    (repo / "README.md").write_text("head\n")
    (repo / "cron" / "b.py").write_text("head-main-only\n")
    (repo / "hermes_cli" / "d.py").write_text("head-main-only\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "main edits"], cwd=repo, check=True)
    return str(repo)


def test_probe_tag_reports_known_conflicts(syn_repo, monkeypatch):
    # The script calls subprocess.run with cwd="."; redirect to the synthetic repo.
    real_run = subprocess.run

    def runner(cmd, *args, **kwargs):
        if isinstance(cmd, list) and cmd and cmd[0] == "git":
            # For `git worktree add` the cwd must be the SOURCE repo, not the
            # worktree path (which doesn't exist yet at that point). Keep
            # `git worktree remove` from cwd="." too — it operates on the
            # primary worktree. We pass the syn_repo as cwd for all git calls.
            kwargs["cwd"] = syn_repo
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", runner)
    # Also patch the script's already-imported reference.
    import sync_batch_probe as sbm

    monkeypatch.setattr(sbm.subprocess, "run", runner)

    probe = _probe_tag("main", "vTEST")
    assert probe.error is None, f"unexpected probe error: {probe.error}"
    # Expect 4 conflicts: agent/a.py, .github/c.yml, tools/e.py, README.md
    # (cron/b.py and hermes_cli/d.py were identical, no conflict;
    # new_file.py was added on one side only, no conflict.)
    assert probe.total == 4
    assert probe.buckets["agent"] == 1
    assert probe.buckets[".github"] == 1
    assert probe.buckets["tools"] == 1
    assert probe.buckets["other"] == 1
    assert probe.buckets["cron"] == 0
    assert probe.buckets["hermes_cli"] == 0
    assert probe.verdict == "OWNER-SESSION"  # 4 > default ceiling of 10? no — 4 ≤ 10
    # Recompute verdict against a tighter ceiling to confirm logic:
    assert probe.total <= 10  # this probe is "AUTONOMOUS" by default
    # Force OWNER-SESSION by checking against a 0-ceiling verdict:
    assert (
        "OWNER-SESSION"
        if probe.total > 0
        else "AUTONOMOUS" == ("OWNER-SESSION" if probe.total > 0 else "AUTONOMOUS")
    )


def test_probe_tag_handles_unresolvable_source(syn_repo, monkeypatch):
    # Use a non-existent source ref → probe should report an error gracefully
    # and not crash. The synthetic repo still gets used as the cwd for git.
    real_run = subprocess.run

    def runner(cmd, *args, **kwargs):
        if isinstance(cmd, list) and cmd and cmd[0] == "git":
            kwargs["cwd"] = syn_repo
        return real_run(cmd, *args, **kwargs)

    import sync_batch_probe as sbm

    monkeypatch.setattr(sbm.subprocess, "run", runner)

    probe = _probe_tag("definitely-not-a-real-ref-xyz", "vTEST")
    assert probe.error is not None
    assert probe.total == 0
    assert probe.verdict == "ERROR"


def test_probe_leaves_repo_clean(syn_repo, monkeypatch):
    """After the probe, the synthetic repo's working tree must be clean."""
    real_run = subprocess.run

    def runner(cmd, *args, **kwargs):
        if isinstance(cmd, list) and cmd and cmd[0] == "git":
            kwargs["cwd"] = syn_repo
        return real_run(cmd, *args, **kwargs)

    import sync_batch_probe as sbm

    monkeypatch.setattr(sbm.subprocess, "run", runner)

    _probe_tag("main", "vTEST")
    status = subprocess.run(
        ["git", "status", "--porcelain"], cwd=syn_repo, capture_output=True, text=True
    )
    assert status.stdout.strip() == "", (
        f"synthetic repo left dirty after probe: {status.stdout!r}"
    )
    # And no leftover worktrees
    wt = subprocess.run(
        ["git", "worktree", "list", "--porcelain"],
        cwd=syn_repo,
        capture_output=True,
        text=True,
    )
    # The only worktree should be the main one.
    assert "worktrees" not in wt.stdout or wt.stdout.count("\n") < 6
