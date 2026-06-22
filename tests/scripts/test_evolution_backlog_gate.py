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
        assert gate.is_bug(_issue("[FEATURE] new thing", labels=["enhancement"])) is False

    def test_improvement_is_not_bug(self):
        assert gate.is_bug(_issue("[IMPROVEMENT] x", labels=["proposal"])) is False


class TestCounting:
    def test_counts_only_features(self):
        issues = [
            _issue("[FEATURE] a", ["proposal"]),
            _issue("[IMPROVEMENT] b", ["enhancement"]),
            _issue("[FIX] c"),                       # bug — excluded
            _issue("broken", ["bug"]),               # bug — excluded
            _issue("[REPLACEMENT] d", ["proposal"]),
        ]
        assert gate.count_open_features(issues) == 3

    def test_should_throttle_at_and_above_cap(self):
        assert gate.should_throttle(25, 25) is True
        assert gate.should_throttle(26, 25) is True
        assert gate.should_throttle(24, 25) is False


class TestCapResolution:
    def test_arg_wins(self, monkeypatch):
        monkeypatch.setenv("EVOLUTION_FEATURE_BACKLOG_CAP", "10")
        assert gate.resolve_cap(30) == 30

    def test_env_used_when_no_arg(self, monkeypatch):
        monkeypatch.setenv("EVOLUTION_FEATURE_BACKLOG_CAP", "10")
        assert gate.resolve_cap(None) == 10

    def test_default_when_nothing(self, monkeypatch):
        monkeypatch.delenv("EVOLUTION_FEATURE_BACKLOG_CAP", raising=False)
        assert gate.resolve_cap(None) == gate.DEFAULT_CAP

    def test_bad_env_falls_back(self, monkeypatch):
        monkeypatch.setenv("EVOLUTION_FEATURE_BACKLOG_CAP", "notanint")
        assert gate.resolve_cap(None) == gate.DEFAULT_CAP


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
        monkeypatch.setattr(gate, "_default_runner", lambda cmd: (0, json.dumps(issues)))
        rc = gate.main(["check", "--cap", "25"])
        out = json.loads(capsys.readouterr().out)
        assert rc == 1 and out["throttle"] is True

    def test_exit_0_when_ok(self, capsys, monkeypatch):
        issues = [_issue("[FEATURE] one", ["proposal"])]
        monkeypatch.setattr(gate, "_default_runner", lambda cmd: (0, json.dumps(issues)))
        rc = gate.main(["check", "--cap", "25"])
        out = json.loads(capsys.readouterr().out)
        assert rc == 0 and out["throttle"] is False
