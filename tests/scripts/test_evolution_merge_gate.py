"""Tests for scripts/evolution_merge_gate.py — the deterministic self-merge policy
(diff-size cap + high-risk path blocklist; the atomic-merge IO is exercised only
via the pure policy here)."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from evolution_merge_gate import (  # noqa: E402
    check_merge_policy,
    check_merge_policy_with_quality,
    _pr_snapshot,
)


def _f(path, additions=1, deletions=0, patch=None):
    f = {"path": path, "additions": additions, "deletions": deletions}
    if patch is not None:
        f["patch"] = patch
    return f


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
        assert any(
            "DIFF_TOO_LARGE" in x for x in check_merge_policy(files, max_lines=10)
        )

    def test_workflow_path_is_high_risk(self):
        v = check_merge_policy([_f(".github/workflows/tests.yml", 2, 1)])
        assert any("HIGH_RISK_PATH" in x for x in v)

    def test_lockfiles_and_manifests_are_high_risk(self):
        for p in (
            "uv.lock",
            "package-lock.json",
            "pyproject.toml",
            "requirements.txt",
            "flake.lock",
        ):
            v = check_merge_policy([_f(p, 3, 1)])
            assert any("HIGH_RISK_PATH" in x for x in v), p

    def test_nested_lockfile_matched_by_basename(self):
        v = check_merge_policy([_f("web/uv.lock", 3, 1)])
        assert any("HIGH_RISK_PATH" in x for x in v)

    def test_own_enforcement_machinery_is_high_risk(self):
        for p in (
            "tools/approval.py",
            "scripts/evolution_merge_gate.py",
            "scripts/register_evolution_cron.py",
        ):
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


class TestCheckMergePolicyWithQuality:
    """Tests for the extended merge policy with test-quality gates (#1209, #1210)."""

    def test_clean_pr_passes_quality_gate(self):
        files = [_f("scripts/foo.py", 30, 5), _f("tests/test_foo.py", 20, 0)]
        assert check_merge_policy_with_quality(files) == []

    def test_high_mock_ratio_blocked(self):
        mock_patch = "+from unittest.mock import MagicMock\n+mock = MagicMock()\n"
        files = [
            _f("tests/test_a.py", 10, patch=mock_patch),
            _f("tests/test_b.py", 10, patch=mock_patch),
        ]
        violations = check_merge_policy_with_quality(files)
        assert any("HIGH_MOCK_RATIO" in v for v in violations)

    def test_fabricated_reproduction_blocked(self):
        content = (
            "def test_fab():\n"
            "    mock = MagicMock()\n"
            "    mock.return_value = 42\n"
            "    assert mock.return_value\n"
        )
        files = [_f("tests/test_fab.py", 10)]
        violations = check_merge_policy_with_quality(
            files, test_contents={"tests/test_fab.py": content}
        )
        assert any("FABRICATED_REPRODUCTION" in v for v in violations)

    def test_diff_too_large_and_mock_ratio_both_reported(self):
        mock_patch = "+from unittest.mock import MagicMock\n"
        files = [
            _f("tests/test_a.py", 150, 60, patch=mock_patch),
        ]
        violations = check_merge_policy_with_quality(files)
        assert any("DIFF_TOO_LARGE" in v for v in violations)
        assert any("HIGH_MOCK_RATIO" in v for v in violations)

    def test_backward_compatible_without_quality_import(self):
        """When test_contents is None and no mock patches, only policy checks run."""
        files = [_f("scripts/foo.py", 30, 5), _f("tests/test_foo.py", 20, 0)]
        violations = check_merge_policy_with_quality(files, test_contents=None)
        assert violations == []


class TestMainWiring:
    """Verify main() calls check_merge_policy_with_quality, not check_merge_policy.

    The closed PR #1212 was rejected because main() still called
    check_merge_policy() — the quality gates were dead code.  This test
    proves the production merge path routes through the quality-aware
    function.
    """

    def test_main_calls_quality_gate(self, monkeypatch):
        """Verify main() routes through check_merge_policy_with_quality.

        The closed PR #1212 was rejected because main() called
        check_merge_policy() directly, making the quality gates dead code.
        This test proves the production merge path invokes the quality-aware
        function.  We only spy on the quality function (not the plain one,
        which is called internally by the quality function — that's expected).
        """
        import evolution_merge_gate as emg

        called = {"quality": False}
        orig_quality = emg.check_merge_policy_with_quality

        def _spy_quality(files, **kw):
            called["quality"] = True
            return orig_quality(files, **kw)

        monkeypatch.setattr(emg, "check_merge_policy_with_quality", _spy_quality)
        # Stub the atomic PR snapshot so main() reaches the policy check with a
        # clean, mergeable diff (files present, head resolved) — post-#1244 the
        # gh IO is _pr_snapshot (files + head in one call), not _pr_files.
        monkeypatch.setattr(
            emg, "_pr_snapshot", lambda pr, repo, runner: ([], "deadbeef")
        )
        monkeypatch.setattr(emg, "_run", lambda cmd: (0, "[]", ""))

        rc = emg.main(["x", "--pr", "999"])
        assert rc == 0
        assert called["quality"], "main() must call check_merge_policy_with_quality"
