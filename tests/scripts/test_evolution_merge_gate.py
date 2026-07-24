"""Tests for scripts/evolution_merge_gate.py — the deterministic self-merge policy
(diff-size cap + high-risk path blocklist; the atomic-merge IO is exercised only
via the pure policy here)."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from evolution_merge_gate import check_merge_policy, _pr_snapshot  # noqa: E402


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

    def test_evolution_self_mod_machinery_is_high_risk(self):
        # The autonomous loop's own job definitions + orchestrator + gate family
        # must require human review (they carry / enforce the safety policy).
        for p in (
            "cron/evolution/integration.yaml",
            "cron/evolution/research.yaml",
            "scripts/evolution_orchestrator.py",
            "scripts/evolution_access_gate.sh",
            "scripts/evolution_hydra_gate.py",
            "scripts/evolution_analysis_gate.sh",
        ):
            v = check_merge_policy([_f(p, 3, 1)])
            assert any("HIGH_RISK_PATH" in x for x in v), p

    def test_integration_skill_is_high_risk_but_other_skills_are_not(self):
        # The integration skill IS the self-merge safety procedure → gated.
        v = check_merge_policy([_f("skills/evolution/evolution-integration/SKILL.md", 5, 2)])
        assert any("HIGH_RISK_PATH" in x for x in v)
        # Other evolution skills stay self-improvable (only CODEOWNERS-gated).
        for p in (
            "skills/evolution/evolution-analysis/SKILL.md",
            "skills/evolution/evolution-implementation/SKILL.md",
        ):
            assert check_merge_policy([_f(p, 5, 2)]) == [], p

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


class TestPrSnapshot:
    """The IO layer must (a) read files + head in ONE gh call so the reviewed
    diff and the merged SHA are the same snapshot (no review→merge TOCTOU), and
    (b) fail CLOSED — a gh error or a response without a proper files list means
    'refuse to merge', never 'empty diff, therefore safe'."""

    def _runner(self, code, payload):
        out = payload if isinstance(payload, str) else json.dumps(payload)
        calls = []

        def runner(cmd):
            calls.append(cmd)
            return code, out, ""

        runner.calls = calls  # type: ignore[attr-defined]
        return runner

    def test_single_call_returns_files_and_head(self):
        runner = self._runner(
            0, {"files": [_f("a.py", 1, 0)], "headRefOid": "deadbeef01"}
        )
        files, head = _pr_snapshot(7, "O/r", runner)
        assert head == "deadbeef01"
        assert files == [_f("a.py", 1, 0)]
        # Exactly ONE gh read, requesting BOTH fields together (the atomic snapshot).
        assert len(runner.calls) == 1  # type: ignore[attr-defined]
        assert "files,headRefOid" in runner.calls[0]  # type: ignore[attr-defined]

    def test_missing_files_key_fails_closed(self):
        # exit 0 but no "files" key — the original fail-open bug (`.get() or []`
        # treated this as an empty, safe diff). Must now refuse.
        runner = self._runner(0, {"headRefOid": "abc"})
        assert _pr_snapshot(7, None, runner) == (None, None)

    def test_files_not_a_list_fails_closed(self):
        runner = self._runner(0, {"files": "oops", "headRefOid": "abc"})
        assert _pr_snapshot(7, None, runner) == (None, None)

    def test_gh_error_fails_closed(self):
        assert _pr_snapshot(7, None, self._runner(1, "")) == (None, None)

    def test_non_json_fails_closed(self):
        assert _pr_snapshot(7, None, self._runner(0, "not json")) == (None, None)

    def test_files_readable_but_head_absent(self):
        runner = self._runner(0, {"files": []})
        files, head = _pr_snapshot(7, None, runner)
        assert files == [] and head is None
