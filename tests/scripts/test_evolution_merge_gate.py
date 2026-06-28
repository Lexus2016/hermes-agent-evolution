"""Tests for scripts/evolution_merge_gate.py — the deterministic self-merge policy
(diff-size cap + high-risk path blocklist; the atomic-merge IO is exercised only
via the pure policy here)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from evolution_merge_gate import check_merge_policy  # noqa: E402


def _f(path, additions=1, deletions=0):
    return {"path": path, "additions": additions, "deletions": deletions}


class TestCheckMergePolicy:
    def test_small_code_change_is_clean(self):
        files = [_f("scripts/foo.py", 30, 5), _f("tests/scripts/test_foo.py", 40, 0)]
        assert check_merge_policy(files) == []

    def test_diff_too_large_flagged(self):
        files = [_f("agent/big.py", 150, 120)]  # 270 > 200
        v = check_merge_policy(files)
        assert any("DIFF_TOO_LARGE" in x for x in v)

    def test_diff_at_cap_is_clean(self):
        files = [_f("agent/x.py", 120, 80)]  # exactly 200
        assert not any("DIFF_TOO_LARGE" in x for x in check_merge_policy(files))

    def test_custom_max_lines(self):
        files = [_f("a.py", 30, 0)]
        assert any("DIFF_TOO_LARGE" in x for x in check_merge_policy(files, max_lines=10))

    def test_workflow_path_is_high_risk(self):
        v = check_merge_policy([_f(".github/workflows/tests.yml", 2, 1)])
        assert any("HIGH_RISK_PATH" in x for x in v)

    def test_lockfiles_and_manifests_are_high_risk(self):
        for p in ("uv.lock", "package-lock.json", "pyproject.toml", "requirements.txt", "flake.lock"):
            v = check_merge_policy([_f(p, 3, 1)])
            assert any("HIGH_RISK_PATH" in x for x in v), p

    def test_nested_lockfile_matched_by_basename(self):
        v = check_merge_policy([_f("web/uv.lock", 3, 1)])
        assert any("HIGH_RISK_PATH" in x for x in v)

    def test_own_enforcement_machinery_is_high_risk(self):
        for p in ("tools/approval.py", "scripts/evolution_merge_gate.py", "scripts/register_evolution_cron.py"):
            v = check_merge_policy([_f(p, 3, 1)])
            assert any("HIGH_RISK_PATH" in x for x in v), p

    def test_secrets_and_env_are_high_risk(self):
        for p in (".env", ".env.production", "config/secret.key", "tls/server.pem"):
            v = check_merge_policy([_f(p, 1, 0)])
            assert any("HIGH_RISK_PATH" in x for x in v), p

    def test_dockerfile_is_high_risk(self):
        v = check_merge_policy([_f("Dockerfile", 2, 0)])
        assert any("HIGH_RISK_PATH" in x for x in v)

    def test_large_and_risky_reports_both(self):
        files = [_f(".github/workflows/x.yml", 1, 0), _f("a.py", 150, 120)]
        v = check_merge_policy(files)
        assert any("DIFF_TOO_LARGE" in x for x in v)
        assert any("HIGH_RISK_PATH" in x for x in v)

    def test_empty_or_non_list_is_safe(self):
        assert check_merge_policy([]) == []
        assert check_merge_policy(None) == []  # type: ignore[arg-type]

    def test_malformed_file_entries_skipped(self):
        files = ["nope", {"path": None}, _f("ok/small.py", 1, 0)]
        assert check_merge_policy(files) == []

    def test_ordinary_docs_and_skill_md_are_clean(self):
        files = [
            _f("skills/evolution/evolution-analysis/SKILL.md", 20, 4),
            _f("docs/note.md", 10, 0),
            _f("agent/feature.py", 60, 10),
        ]
        assert check_merge_policy(files) == []
