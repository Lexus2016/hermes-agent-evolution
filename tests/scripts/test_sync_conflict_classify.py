"""Offline real-Git contracts for all-path conflict evidence (#3456)."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.sync_conflict_classify import classify, _read_paths_file

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "sync_classify.py"


def git(*args):
    return subprocess.run(
        ["git", *args],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    ).stdout.strip()


@pytest.fixture()
def repo(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    git("init", "-q", "-b", "main")
    git("config", "user.name", "Test")
    git("config", "user.email", "test@example.invalid")
    git("config", "commit.gpgsign", "false")
    git("config", "core.filemode", "false")
    paths = [
        "agent/a",
        "cron/b",
        ".github/c",
        "hermes_cli/d",
        "tools/e",
        "space ü.txt",
        "mode",
    ]
    for p in paths:
        Path(p).parent.mkdir(parents=True, exist_ok=True)
        Path(p).write_bytes(b"base\x00\xff")
    Path("tests").mkdir()
    Path("tests/test_removed.py").write_text(
        "def test_example():\n    assert True\n", encoding="utf-8"
    )
    git("add", ".")
    git("commit", "-qm", "base")
    git("tag", "base")
    Path("tests/test_removed.py").write_text(
        "def test_example():\n    assert False\n", encoding="utf-8"
    )
    Path("fork-only").write_bytes(b"new fork")
    for p in ("cron/b", ".github/c", "space ü.txt"):
        Path(p).write_bytes(b"fork\x00\xfe")
    Path("tools/e").unlink()
    git("add", "-A")
    git("update-index", "--chmod=+x", "mode")
    git("commit", "-qm", "fork")
    git("tag", "fork")
    git("checkout", "-qb", "upstream", "base")
    for p in ("agent/a", ".github/c", "space ü.txt"):
        Path(p).write_bytes(b"upstream\x00\xfd")
    Path("tests/test_removed.py").unlink()
    Path("hermes_cli/d").unlink()
    Path("upstream-only").write_bytes(b"new upstream")
    git("add", "-A")
    git("commit", "-qm", "upstream")
    return [*paths, "fork-only", "upstream-only"]


def test_cli_preserves_binary_mode_deletion_evidence_and_counts(repo, tmp_path):
    paths = tmp_path / "paths.nul"
    paths.write_text("\0".join([*repo, repo[0]]) + "\0", encoding="utf-8")
    out = tmp_path / "out"
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--base",
            "base",
            "--fork",
            "fork",
            "--upstream",
            "upstream",
            "--paths-file",
            str(paths),
            "--out-dir",
            str(out),
        ],
        cwd=tmp_path / ".github",
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert result.returncode == 1, result.stderr
    report = json.loads(
        (out / "sync-conflicts-upstream.json").read_text(encoding="utf-8")
    )
    expected = dict(
        zip(
            repo,
            [
                "upstream-wins",
                "fork-keeps",
                "manual",
                "upstream-wins",
                "fork-keeps",
                "manual",
                "fork-keeps",
                "fork-keeps",
                "upstream-wins",
            ],
        )
    )
    assert {v["path"]: v["verdict"] for v in report["verdicts"]} == expected
    assert report["summary"]["total"] == len(expected)
    assert sum(
        sum(counts.values()) for counts in report["summary"]["by_bucket"].values()
    ) == len(expected)
    for v in report["verdicts"]:
        assert v["base_exists"] == (v["base_entry"] is not None)
        assert v["fork_exists"] == (v["fork_entry"] is not None)
        assert v["upstream_exists"] == (v["upstream_entry"] is not None)
    assert report["fork"] == git("rev-parse", "fork")
    markdown = (out / "sync-conflicts-upstream.md").read_text(encoding="utf-8")
    for path, verdict in expected.items():
        assert f"`{path}` | {verdict}" in markdown
    legacy = subprocess.run(
        [sys.executable, str(SCRIPT), "--base", "base", "--fork", "fork", "tests/"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert legacy.returncode == 1
    assert "FORK-MODIFIED 1" in legacy.stdout
    assert not git("diff", "--name-only")  # no working-tree edits by classification


def test_invalid_refs_fail_closed_and_empty_list_is_valid(repo, tmp_path):
    with pytest.raises(subprocess.CalledProcessError):
        classify(repo, "base", "missing-ref", "upstream")
    with pytest.raises(ValueError, match="absent from all three trees"):
        classify(["misspelled-path"], "base", "fork", "upstream")
    assert classify([], "base", "fork", "upstream") == []
    paths_file = tmp_path / "paths.nul"
    paths_file.write_bytes(b"name\rwith-cr\0name\nwith-lf\0invalid-utf8-\xff\0")
    assert _read_paths_file(str(paths_file)) == [
        "name\rwith-cr",
        "name\nwith-lf",
        "invalid-utf8-\udcff",
    ]
    paths_file.write_bytes(b" spaced path \r\nsecond\n")
    assert _read_paths_file(str(paths_file)) == [" spaced path ", "second"]
