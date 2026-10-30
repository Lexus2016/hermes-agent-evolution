"""Tests for scripts/evolution_merge_gate.py — the deterministic self-merge policy
(diff-size cap + high-risk path blocklist; the atomic-merge IO is exercised only
via the pure policy here)."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from evolution_merge_gate import (  # noqa: E402
    check_flip_gate,
    check_merge_method_policy,
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
        v = check_merge_policy([
            _f("skills/evolution/evolution-integration/SKILL.md", 5, 2)
        ])
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
        files, head, branch = _pr_snapshot(7, "O/r", runner)
        assert head == "deadbeef01"
        assert files == [_f("a.py", 1, 0)]
        assert branch == ""  # absent headRefName degrades to "" (not an error)
        # Exactly ONE gh read, requesting ALL fields together (the atomic snapshot).
        assert len(runner.calls) == 1  # type: ignore[attr-defined]
        assert any(
            "files,headRefOid" in a for a in runner.calls[0]  # type: ignore[attr-defined]
        )

    def test_missing_files_key_fails_closed(self):
        # exit 0 but no "files" key — the original fail-open bug (`.get() or []`
        # treated this as an empty, safe diff). Must now refuse.
        runner = self._runner(0, {"headRefOid": "abc"})
        assert _pr_snapshot(7, None, runner)[:2] == (None, None)

    def test_files_not_a_list_fails_closed(self):
        runner = self._runner(0, {"files": "oops", "headRefOid": "abc"})
        assert _pr_snapshot(7, None, runner)[:2] == (None, None)

    def test_gh_error_fails_closed(self):
        assert _pr_snapshot(7, None, self._runner(1, ""))[:2] == (None, None)

    def test_non_json_fails_closed(self):
        assert _pr_snapshot(7, None, self._runner(0, "not json"))[:2] == (None, None)

    def test_files_readable_but_head_absent(self):
        runner = self._runner(0, {"files": []})
        files, head, _branch = _pr_snapshot(7, None, runner)
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
            emg, "_pr_snapshot", lambda pr, repo, runner: ([], "deadbeef", "feature/quality-wiring")
        )
        monkeypatch.setattr(emg, "_run", lambda cmd: (0, "[]", ""))

        rc = emg.main(["x", "--pr", "999"])
        assert rc == 0
        assert called["quality"], "main() must call check_merge_policy_with_quality"


# ── #1246: fail-closed on quality-gate import + live fabrication check ──────


class TestQualityGateFailClosed:
    """#1246: the test-quality gate is a self-merge safety control. If its
    module can't be imported, the merge must be BLOCKED, not silently allowed."""

    def test_import_failure_blocks_merge(self, monkeypatch):
        import types as _types
        import evolution_merge_gate as emg

        # Simulate a broken deploy: module present but missing check_test_quality
        broken = _types.ModuleType("evolution_test_quality_gate")
        monkeypatch.setitem(sys.modules, "evolution_test_quality_gate", broken)

        violations = emg.check_merge_policy_with_quality([_f("scripts/foo.py", 10, 0)])
        assert any("TEST_QUALITY_GATE_UNAVAILABLE" in v for v in violations), violations


class TestFetchTestContents:
    """#1246: main() folds changed test-file contents (at the reviewed head SHA)
    into the gate so the fabricated-reproduction check runs on the live path."""

    def _runner(self, mapping):
        def runner(cmd):
            url = cmd[2] if len(cmd) > 2 else ""
            for key, (code, content) in mapping.items():
                if key in url:
                    return code, content, ""
            return 1, "", "not found"

        return runner

    def test_fetches_and_decodes_test_files_only(self):
        import base64 as _b64
        import evolution_merge_gate as emg

        content = "def test_x():\n    assert True\n"
        enc = _b64.b64encode(content.encode()).decode()
        runner = self._runner({"tests/test_x.py": (0, enc)})
        files = [_f("tests/test_x.py", 5, 0), _f("scripts/foo.py", 3, 0)]
        out = emg._fetch_test_contents(files, "deadbeef", "O/r", runner)
        assert out == {"tests/test_x.py": content}  # non-test file skipped

    def test_no_head_or_slug_returns_empty(self):
        import evolution_merge_gate as emg

        runner = self._runner({})
        assert (
            emg._fetch_test_contents([_f("tests/test_x.py")], None, "O/r", runner) == {}
        )
        assert (
            emg._fetch_test_contents([_f("tests/test_x.py")], "sha", "", runner) == {}
        )

    def test_gh_error_is_best_effort_omit(self):
        import evolution_merge_gate as emg

        runner = self._runner({})  # every fetch returns code 1
        out = emg._fetch_test_contents([_f("tests/test_x.py")], "sha", "O/r", runner)
        assert out == {}

    def test_looks_like_test_predicate(self):
        import evolution_merge_gate as emg

        assert emg._looks_like_test("tests/test_a.py")
        assert emg._looks_like_test("pkg/tests/test_b.py")
        assert emg._looks_like_test("foo/test_c.py")
        assert emg._looks_like_test("foo/d_test.py")
        assert not emg._looks_like_test("scripts/foo.py")


# ── #1447: flip-gate wiring into merge verification ──────────────────────────


class TestCheckFlipGate:
    """Tests for the flip-gate wiring (#1447, Child C of #1308).

    Success criteria: P→F regression prevents merge, blocking comment names
    specific probes, override leaves recorded reason, skills without probes
    merge as before.
    """

    def test_no_probe_set_skips_gate(self):
        """Skills without probe sets are unaffected — opt-in per skill."""
        assert check_flip_gate() == []
        assert check_flip_gate(None, None) == []

    def test_regression_blocks_and_names_probes(self):
        """P→F regression prevents merge; violation names the regressed probe."""
        v = check_flip_gate(
            before={"probe_a": True, "probe_b": True},
            after={"probe_a": False, "probe_b": True},
        )
        assert len(v) == 1
        assert "FLIP_GATE_BLOCK" in v[0]
        assert "probe_a" in v[0]
        assert "regressed: probe_a" in v[0]  # per-example table in the comment

    def test_override_records_reason(self):
        """Override path downgrades block to a recorded warning with reason."""
        v = check_flip_gate(
            before={"probe_a": True},
            after={"probe_a": False},
            override_reason="Known flaky probe, manual review approved",
        )
        assert "FLIP_GATE_OVERRIDE" in v[0]
        assert "Known flaky probe, manual review approved" in v[0]
        assert "FLIP_GATE_BLOCK" not in v[0]

    def test_empty_override_does_not_downgrade(self):
        """An empty override string does not downgrade the block."""
        v = check_flip_gate(
            before={"probe_a": True}, after={"probe_a": False}, override_reason=""
        )
        assert "FLIP_GATE_BLOCK" in v[0]


class TestFlipGateWiringInQualityGate:
    """Verify check_merge_policy_with_quality wires the flip gate correctly."""

    def test_no_flip_gate_results_unaffected(self):
        """Skills without probe sets merge as before (flip_gate_results=None)."""
        files = [_f("scripts/foo.py", 30, 5), _f("tests/test_foo.py", 20, 0)]
        assert check_merge_policy_with_quality(files, flip_gate_results=None) == []

    def test_regression_prevents_merge(self):
        """P→F regression prevents merge through the quality gate."""
        files = [_f("scripts/foo.py", 30, 5)]
        v = check_merge_policy_with_quality(
            files,
            flip_gate_results={
                "before": {"probe_a": True},
                "after": {"probe_a": False},
            },
        )
        assert any("FLIP_GATE_BLOCK" in x for x in v)
        assert any("probe_a" in x for x in v)

    def test_override_leaves_recorded_reason(self):
        """Override path records a reason instead of blocking."""
        files = [_f("scripts/foo.py", 30, 5)]
        v = check_merge_policy_with_quality(
            files,
            flip_gate_results={
                "before": {"probe_a": True},
                "after": {"probe_a": False},
            },
            flip_gate_override="Manual override: probe deprecated",
        )
        assert any("FLIP_GATE_OVERRIDE" in x for x in v)
        assert not any("FLIP_GATE_BLOCK" in x for x in v)

    def test_no_data_fails_closed(self):
        """When flip_gate_results is provided but both sides are None, fail closed."""
        files = [_f("scripts/foo.py", 30, 5)]
        v = check_merge_policy_with_quality(
            files, flip_gate_results={"before": None, "after": None}
        )
        assert any("FLIP_GATE_NO_DATA" in x for x in v)

    def test_clean_flip_gate_with_clean_policy_passes(self):
        """Both policy and flip gate clean → no violations."""
        files = [_f("scripts/foo.py", 30, 5)]
        assert (
            check_merge_policy_with_quality(
                files,
                flip_gate_results={
                    "before": {"a": True, "b": False},
                    "after": {"a": True, "b": True},
                },
            )
            == []
        )


# ── #2498: Reachability gate integration ────────────────────────────────────


class TestReachabilityIntegration:
    def test_reachability_violation_in_merge_gate(self):
        files = [_f("agent/unwired_feature.py", 30, 0)]
        source_contents = {
            "agent/unwired_feature.py": "class UnwiredFeature:\n    pass\n",
        }
        violations = check_merge_policy_with_quality(
            files,
            source_contents=source_contents,
        )
        assert any("DEAD_CODE_UNREACHABLE" in v for v in violations)
        assert any("UnwiredFeature" in v for v in violations)

    def test_reachability_clean_passes(self):
        files = [_f("agent/wired_feature.py", 30, 0)]
        source_contents = {
            "agent/wired_feature.py": "class WiredFeature:\n    pass\n",
            "run_agent.py": "from agent.wired_feature import WiredFeature\n",
        }
        violations = check_merge_policy_with_quality(
            files,
            source_contents=source_contents,
        )
        assert violations == []


class TestCheckMergeMethodPolicy:
    """#2783: sync/* PRs must land as true merge commits, never squash."""

    def test_sync_branch_squash_blocked(self):
        v = check_merge_method_policy("sync/upstream-v2026.8.16.2", "squash")
        assert v and "MERGE_METHOD" in v[0] and "merge" in v[0]

    def test_sync_branch_rebase_blocked(self):
        assert check_merge_method_policy("sync/upstream-release-x", "rebase")

    def test_sync_branch_true_merge_allowed(self):
        assert check_merge_method_policy("sync/upstream-release-x", "merge") == []

    def test_evolution_branch_keeps_squash_default(self):
        assert check_merge_method_policy("evolution/issue-1-foo", "squash") == []

    def test_missing_branch_is_not_sync(self):
        # headRefName absent (degraded snapshot) — never matches sync/, so the
        # method policy opts out rather than blocking on missing data.
        assert check_merge_method_policy(None, "squash") == []
        assert check_merge_method_policy("", "squash") == []


# ── #3517: the replay gate must be reachable from the merge path ───────


class TestShippedReplayGate:
    """A replay gate nothing calls is dead code. A PR that ships
    ``.evolution/replay-record.json`` is replayed at the reviewed head SHA by the
    pre-merge path, and a replay that misses its recorded grade keeps the PR out
    of the merge path instead of being reported and ignored."""

    def _trace_text(self, *statuses):
        from evolution_trajectory_logger import TrajectoryLog

        lines = []
        for i, status in enumerate(statuses):
            log = TrajectoryLog(session_id="s1", completed=True)
            log.add_tool_call(f"tool_{i}", {"a": i}, result="ok", status=status)
            lines.append(json.dumps(log.to_dict(), sort_keys=True))
        return "\n".join(lines) + "\n"

    def _wire(
        self,
        monkeypatch,
        tmp_path=None,
        *,
        trace_status="success",
        record_grade="pass",
        branch="fix/replay-wiring",
        record=None,
    ):
        import base64 as _b64
        import evolution_merge_gate as emg

        # Keep the floor-gate default path out of the real hermes home.
        floor = (tmp_path or Path(".")) / "floor_scores.json"
        monkeypatch.setenv("EVOLUTION_FLOOR_SCORES", str(floor))

        payload = record
        if payload is None:
            payload = {
                "kind": "trace",
                "path": "evidence/turns.jsonl",
                "grade": record_grade,
            }
        contents = {
            ".evolution/replay-record.json": _b64.b64encode(
                json.dumps(payload).encode()
            ).decode(),
            "evidence/turns.jsonl": _b64.b64encode(
                self._trace_text("success", trace_status).encode()
            ).decode(),
        }
        calls = []

        def runner(cmd):
            calls.append(cmd)
            joined = " ".join(cmd)
            for path, enc in contents.items():
                if f"contents/{path}?ref=" in joined:
                    return 0, enc, ""
            if api_merge in joined:
                return 0, "{}", ""
            return 1, "", "not found"

        api_merge = "repos/O/r/pulls/42/merge"
        files = [
            _f(".evolution/replay-record.json", 4, 0),
            _f("evidence/turns.jsonl", 6, 0),
            _f("scripts/replay_wiring.py", 40, 2),
            _f("tests/scripts/test_replay_wiring.py", 30, 0),
        ]
        monkeypatch.setattr(
            emg,
            "_pr_snapshot",
            lambda pr, repo, runner: (files, "deadbeefcafe", branch),
        )
        monkeypatch.setattr(emg, "_run", runner)
        return emg, calls

    @staticmethod
    def _merge_calls(calls):
        return [c for c in calls if "/pulls/42/merge" in " ".join(c)]

    def test_failed_replay_blocks_the_merge(self, monkeypatch, capsys, tmp_path):
        emg, calls = self._wire(
            monkeypatch, tmp_path, trace_status="failure", record_grade="pass"
        )
        rc = emg.main(["x", "--pr", "42", "--merge", "--repo", "O/r"])
        out = capsys.readouterr().out
        assert rc == 1, out
        assert "REPLAY_NOT_REPRODUCED" in out
        assert self._merge_calls(calls) == [], "a failed replay must not reach the merge API"

    def test_unfinished_session_blocks_the_merge(self, monkeypatch, capsys, tmp_path):
        emg, calls = self._wire(
            monkeypatch, tmp_path, trace_status="pending", record_grade="pass"
        )
        rc = emg.main(["x", "--pr", "42", "--merge", "--repo", "O/r"])
        out = capsys.readouterr().out
        assert rc == 1, out
        assert "REPLAY_NOT_REPLAYABLE" in out
        assert "replay grade fail" not in out
        assert self._merge_calls(calls) == []

    def test_reproduced_replay_merges_at_reviewed_head(self, monkeypatch, capsys, tmp_path):
        emg, calls = self._wire(
            monkeypatch, tmp_path, trace_status="success", record_grade="pass"
        )
        rc = emg.main(["x", "--pr", "42", "--merge", "--repo", "O/r"])
        out = capsys.readouterr().out
        assert rc == 0, out
        merge = self._merge_calls(calls)
        assert merge, calls
        assert "sha=deadbeefcafe" in merge[0]

    def test_pr_without_record_skips_the_replay_step(self):
        import evolution_merge_gate as emg

        def runner(cmd):  # pragma: no cover - must not be reached
            raise AssertionError(f"unexpected gh call: {cmd}")

        files = [_f("scripts/replay_wiring.py", 40, 2)]
        assert emg._check_shipped_replay(files, "sha", "O/r", runner) == []

    def test_unreadable_record_fails_closed(self):
        import evolution_merge_gate as emg

        files = [_f(emg.REPLAY_RECORD_PATH, 4, 0)]
        out = emg._check_shipped_replay(files, "sha", "O/r", lambda c: (1, "", ""))
        assert out and out[0].startswith("REPLAY_UNVERIFIED")

    def test_malformed_record_json_fails_closed(self):
        import base64 as _b64
        import evolution_merge_gate as emg

        files = [_f(emg.REPLAY_RECORD_PATH, 4, 0)]
        out = emg._check_shipped_replay(
            files, "sha", "O/r", lambda c: (0, _b64.b64encode(b"not json").decode(), "")
        )
        assert out and "not JSON" in out[0]

    def test_missing_head_fails_closed(self):
        import evolution_merge_gate as emg

        files = [_f(emg.REPLAY_RECORD_PATH, 4, 0)]
        out = emg._check_shipped_replay(files, None, "O/r", lambda c: (0, "", ""))
        assert out and out[0].startswith("REPLAY_UNVERIFIED")
