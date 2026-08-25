"""Tests for the misevolution guard audit (issue #3191)."""

import json
import subprocess

from scripts.evolution_misevolution_audit import (
    audit_merged_commits,
    guard_weakening_flags,
    run_audit,
    volume_drift_flag,
)


def _git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True)


class TestGuardWeakening:
    def test_flag_semantics(self):
        assert guard_weakening_flags(
            "-DEFAULT_MAX_LINES = 200\n+DEFAULT_MAX_LINES = 1000\n"
        ) == ["DEFAULT_MAX_LINES"]
        # Context/header/unrelated lines are never flagged.
        assert guard_weakening_flags(" DEFAULT_X = 1\n--- a/y\n+++ b/y\n") == []


class TestVolumeDrift:
    def test_drift_semantics(self):
        flag = volume_drift_flag(10, 20, 5)
        assert flag is not None and "0.25" in flag
        assert volume_drift_flag(10, 20, 15) is None  # healthy rate
        assert volume_drift_flag(20, 5, 1) is None  # volume shrank
        assert volume_drift_flag(0, 0, 0) is None  # nothing proposed


class TestRunAudit:
    def test_scans_real_repo_history_for_guard_edits(self, tmp_path):
        for args in (
            ["init", "-q"],
            ["config", "user.email", "t@t"],
            ["config", "user.name", "t"],
        ):
            _git(tmp_path, *args)
        (tmp_path / "scripts").mkdir()
        f = tmp_path / "scripts" / "evolution_merge_gate.py"
        f.write_text("DEFAULT_MAX_LINES = 200\n", encoding="utf-8")
        _git(tmp_path, "add", "-A")
        _git(tmp_path, "commit", "-qm", "feat: evolution gate init")
        f.write_text("DEFAULT_MAX_LINES = 5000\n", encoding="utf-8")
        _git(tmp_path, "commit", "-am", "evolution: relax gate")
        _git(tmp_path, "branch", "origin/main")

        flags = audit_merged_commits(tmp_path, count=5)
        assert flags and "relax gate" in flags[0]

    def test_metrics_volume_drift_is_surfaced(self, tmp_path):
        metrics = tmp_path / "m.json"
        windows = {
            "windows": [{"proposals": 10, "merged": 8}, {"proposals": 30, "merged": 3}]
        }
        metrics.write_text(json.dumps(windows), encoding="utf-8")
        verdict = run_audit(tmp_path, metrics)
        assert not verdict["ok"]
        assert any("proxy-metric" in flag for flag in verdict["flags"])

    def test_unavailable_git_is_reported_not_raised(self, tmp_path):
        (tmp_path / ".git").write_text("gitdir: nowhere\n", encoding="utf-8")
        verdict = run_audit(tmp_path, None)
        assert any("unavailable" in flag for flag in verdict["flags"])
