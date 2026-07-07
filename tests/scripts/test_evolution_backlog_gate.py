"""Tests for scripts/evolution_backlog_gate.py — throttle features, never bugs."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

import evolution_backlog_gate as gate  # noqa: E402


def _issue(title, labels=()):
    return {"title": title, "labels": [{"name": n} for n in labels]}


class TestIsBug:
    def test_fix_title_is_bug(self):
        assert gate.is_bug(_issue("[FIX] tool crashes")) is True

    def test_fix_title_case_insensitive(self):
        assert gate.is_bug(_issue("[fix] lowercase")) is True

    def test_bug_label_is_bug(self):
        assert gate.is_bug(_issue("something broken", labels=["bug"])) is True

    def test_feature_is_not_bug(self):
        assert (
            gate.is_bug(_issue("[FEATURE] new thing", labels=["enhancement"])) is False
        )

    def test_improvement_is_not_bug(self):
        assert gate.is_bug(_issue("[IMPROVEMENT] x", labels=["proposal"])) is False


class TestCounting:
    def test_counts_only_features(self):
        issues = [
            _issue("[FEATURE] a", ["proposal"]),
            _issue("[IMPROVEMENT] b", ["enhancement"]),
            _issue("[FIX] c"),  # bug — excluded
            _issue("broken", ["bug"]),  # bug — excluded
            _issue("[REPLACEMENT] d", ["proposal"]),
        ]
        assert gate.count_open_features(issues) == 3

    def test_should_throttle_at_and_above_cap(self):
        assert gate.should_throttle(25, 25) is True
        assert gate.should_throttle(26, 25) is True
        assert gate.should_throttle(24, 25) is False


class TestCapResolution:
    def test_arg_wins(self, monkeypatch, tmp_path):
        monkeypatch.setenv("EVOLUTION_FEATURE_BACKLOG_CAP", "10")
        monkeypatch.setenv("EVOLUTION_PROFILE_DIR", str(tmp_path))
        assert gate.resolve_cap(30) == 30

    def test_env_used_when_no_arg(self, monkeypatch, tmp_path):
        monkeypatch.setenv("EVOLUTION_FEATURE_BACKLOG_CAP", "10")
        monkeypatch.setenv("EVOLUTION_PROFILE_DIR", str(tmp_path))
        assert gate.resolve_cap(None) == 10

    def test_default_when_nothing(self, monkeypatch, tmp_path):
        monkeypatch.delenv("EVOLUTION_FEATURE_BACKLOG_CAP", raising=False)
        monkeypatch.setenv("EVOLUTION_PROFILE_DIR", str(tmp_path))
        assert gate.resolve_cap(None) == gate.DEFAULT_CAP

    def test_bad_env_falls_back(self, monkeypatch, tmp_path):
        monkeypatch.setenv("EVOLUTION_FEATURE_BACKLOG_CAP", "notanint")
        monkeypatch.setenv("EVOLUTION_PROFILE_DIR", str(tmp_path))
        assert gate.resolve_cap(None) == gate.DEFAULT_CAP


class TestDynamicCap:
    """Dynamic cap shrinks when integration is stuck (MERGED_ZERO xN) and never
    overrides explicit --cap or env EVOLUTION_FEATURE_BACKLOG_CAP."""

    def _record(self, merged, selected=1, created=0):
        return {"merged": merged, "selected": selected, "issues_created": created}

    def test_streak_zero_returns_base_cap(self):
        records = [self._record(1), self._record(1), self._record(1)]
        assert gate.resolve_dynamic_cap(records) == gate.DEFAULT_CAP

    def test_streak_three_shrinks_to_eight(self):
        records = [self._record(1), self._record(0), self._record(0), self._record(0)]
        assert gate.resolve_dynamic_cap(records) == 8

    def test_streak_five_shrinks_to_five(self):
        records = [self._record(0)] * 5
        assert gate.resolve_dynamic_cap(records) == 5

    def test_low_success_rate_shrinks_to_eight(self):
        # 7 active cycles, only 1 merge -> success rate 1/7 < 1/3
        records = [self._record(0)] * 6 + [self._record(1)]
        assert gate.resolve_dynamic_cap(records) == 8

    def test_resolve_cap_prefers_arg_over_dynamic(self, tmp_path):
        metrics = tmp_path / "metrics.jsonl"
        metrics.write_text(
            "\n".join(json.dumps(self._record(0)) for _ in range(5)) + "\n"
        )
        assert gate.resolve_cap(100, evolution_dir=tmp_path) == 100

    def test_resolve_cap_prefers_env_over_dynamic(self, monkeypatch, tmp_path):
        metrics = tmp_path / "metrics.jsonl"
        metrics.write_text(
            "\n".join(json.dumps(self._record(0)) for _ in range(5)) + "\n"
        )
        monkeypatch.setenv("EVOLUTION_FEATURE_BACKLOG_CAP", "12")
        assert gate.resolve_cap(None, evolution_dir=tmp_path) == 12

    def test_resolve_cap_uses_dynamic_when_no_override(self, tmp_path):
        metrics = tmp_path / "metrics.jsonl"
        metrics.write_text(
            "\n".join(json.dumps(self._record(0)) for _ in range(5)) + "\n"
        )
        assert gate.resolve_cap(None, evolution_dir=tmp_path) == 5

    def test_dynamic_cap_ignores_idle_cycles_for_success_rate(self):
        # Idle cycles (selected=0, created=0) must not dilute the success rate.
        records = [self._record(0, selected=0, created=0)] * 5 + [self._record(1)]
        assert gate.resolve_dynamic_cap(records) == gate.DEFAULT_CAP

    def test_dynamic_cap_never_throttles_bugs(self):
        # Cap value itself is computed without issue type; throttling decision is
        # made later by count_open_features. A tiny cap with only bugs still ok.
        records = [self._record(0)] * 5
        issues = [
            _issue("[FIX] a"),
            _issue("[FIX] b"),
            _issue("broken", labels=["bug"]),
        ]

        def runner(cmd):
            return 0, json.dumps(issues)

        r = gate.evaluate(gate.resolve_dynamic_cap(records), runner=runner)
        assert r["throttle"] is False and r["open_features"] == 0

    def test_empty_metrics_falls_back_to_base_cap(self):
        assert gate.resolve_dynamic_cap([]) == gate.DEFAULT_CAP

    def test_arg_cap_disables_dynamic_in_cli(self, capsys, monkeypatch, tmp_path):
        metrics = tmp_path / "metrics.jsonl"
        metrics.write_text(
            "\n".join(json.dumps(self._record(0)) for _ in range(5)) + "\n"
        )
        monkeypatch.setenv("EVOLUTION_PROFILE_DIR", str(tmp_path))
        monkeypatch.setattr(gate, "_default_runner", lambda cmd: (0, json.dumps([])))
        rc = gate.main(["check", "--cap", "25"])
        out = json.loads(capsys.readouterr().out)
        assert rc == 0 and out["cap"] == 25


class TestEvaluate:
    def _runner(self, issues, rc=0):
        def run(cmd):
            return rc, json.dumps(issues)

        return run

    def test_throttles_when_over_cap(self):
        issues = [_issue(f"[FEATURE] {i}", ["proposal"]) for i in range(30)]
        r = gate.evaluate(25, runner=self._runner(issues))
        assert r["throttle"] is True and r["open_features"] == 30

    def test_ok_when_under_cap(self):
        issues = [_issue(f"[FEATURE] {i}", ["proposal"]) for i in range(5)]
        r = gate.evaluate(25, runner=self._runner(issues))
        assert r["throttle"] is False and r["open_features"] == 5

    def test_bugs_do_not_count_toward_cap(self):
        issues = [_issue(f"[FIX] bug {i}") for i in range(40)] + [
            _issue("[FEATURE] one", ["proposal"])
        ]
        r = gate.evaluate(25, runner=self._runner(issues))
        # 40 bugs + 1 feature → only 1 feature → not throttled
        assert r["open_features"] == 1 and r["throttle"] is False

    def test_fails_open_when_gh_errors(self):
        def run(cmd):
            return 1, "error: not authenticated"

        r = gate.evaluate(25, runner=run)
        assert r["throttle"] is False  # never block on a failed count

    def test_fails_open_on_garbage(self):
        def run(cmd):
            return 0, "not json"

        r = gate.evaluate(25, runner=run)
        assert r["throttle"] is False


class TestCLI:
    def test_exit_1_when_throttled(self, capsys, monkeypatch):
        issues = [_issue(f"[FEATURE] {i}", ["proposal"]) for i in range(30)]
        monkeypatch.setattr(
            gate, "_default_runner", lambda cmd: (0, json.dumps(issues))
        )
        rc = gate.main(["check", "--cap", "25"])
        out = json.loads(capsys.readouterr().out)
        assert rc == 1 and out["throttle"] is True

    def test_exit_0_when_ok(self, capsys, monkeypatch):
        issues = [_issue("[FEATURE] one", ["proposal"])]
        monkeypatch.setattr(
            gate, "_default_runner", lambda cmd: (0, json.dumps(issues))
        )
        rc = gate.main(["check", "--cap", "25"])
        out = json.loads(capsys.readouterr().out)
        assert rc == 0 and out["throttle"] is False
