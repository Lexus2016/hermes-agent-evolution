# -*- coding: utf-8 -*-
"""Tests for the leave-one-out utility audit (issue #2286, SkillProx)."""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from evolution_utility_audit import (  # noqa: E402
    KEEP_FRACTION,
    apply_demotions,
    audit_corpus,
    main,
    retrieval_precision,
    skill_utility,
)


def _now():
    return datetime.now(timezone.utc)


def _rec(use=0, view=0, patch=0, last=None, description=""):
    d: dict = {"use_count": use, "view_count": view, "patch_count": patch}
    if last is not None:
        d["last_used_at"] = last.isoformat()
    if description:
        d["description"] = description
    return d


class TestSkillUtility:
    def test_zero_activity_scores_zero(self):
        assert skill_utility(_rec()) == 0.0

    def test_recent_activity_scores_full(self):
        now = _now()
        assert skill_utility(_rec(use=10, last=now), now) == 10.0

    def test_high_utility_skill_is_kept(self):
        now = _now()
        usage = {"workhorse": _rec(use=100, last=now), "minor": _rec(use=1, last=now)}
        by_name = {a.name: a for a in audit_corpus(usage, now)}
        assert by_name["workhorse"].verdict == "keep"
        assert by_name["workhorse"].share >= KEEP_FRACTION

    def test_inert_skill_is_removed(self):
        now = _now()
        usage = {"active": _rec(use=50, last=now), "inert": _rec()}
        by_name = {a.name: a for a in audit_corpus(usage, now)}
        assert by_name["inert"].verdict == "remove"

    def test_redundant_low_utility_is_consolidated(self):
        now = _now()
        usage = {
            "main": _rec(use=100, last=now),
            "parse-json": _rec(
                use=1, last=now, description="parse json data structures"
            ),
            "json-parse": _rec(
                use=1, last=now, description="parse json data structures"
            ),
        }
        by_name = {a.name: a for a in audit_corpus(usage, now)}
        assert by_name["parse-json"].max_overlap > 0.35
        assert by_name["parse-json"].verdict == "consolidate"

    def test_low_utility_non_redundant_is_demoted(self):
        now = _now()
        usage = {
            "main": _rec(use=100, last=now),
            "niche": _rec(
                use=1, last=now, description="completely unrelated domain topic"
            ),
        }
        by_name = {a.name: a for a in audit_corpus(usage, now)}
        assert by_name["niche"].verdict == "demote"


class TestApplyDemotions:
    def test_demote_verdict_stamps_trust_state(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        usage = {
            "main": _rec(use=100, last=_now()),
            "niche": _rec(use=1, last=_now(), description="unrelated topic"),
        }
        audits = audit_corpus(usage)
        demoted = apply_demotions(audits, usage)
        assert "niche" in demoted
        assert usage["niche"]["trust_state"] == "demoted"
        assert "demoted_at" in usage["niche"]

    def test_keep_verdict_not_demoted(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        usage = {"workhorse": _rec(use=100, last=_now())}
        audits = audit_corpus(usage)
        demoted = apply_demotions(audits, usage)
        assert demoted == []
        assert usage["workhorse"].get("trust_state") is None

    def test_already_demoted_is_idempotent(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        usage = {
            "main": _rec(use=100, last=_now()),
            "niche": _rec(use=1, last=_now(), description="unrelated topic"),
        }
        audits = audit_corpus(usage)
        apply_demotions(audits, usage)
        demoted_again = apply_demotions(audits, usage)
        assert demoted_again == []

    def test_persists_to_sidecar(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        usage = {
            "main": _rec(use=100, last=_now()),
            "niche": _rec(use=1, last=_now(), description="unrelated topic"),
        }
        audits = audit_corpus(usage)
        apply_demotions(audits, usage)
        sidecar = tmp_path / "skills" / ".usage.json"
        assert sidecar.exists()
        saved = json.loads(sidecar.read_text(encoding="utf-8"))
        assert saved["niche"]["trust_state"] == "demoted"


class TestCli:
    def test_report_only_by_default(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        assert main([]) == 0
        out = capsys.readouterr().out
        assert "utility-audit" in out

    def test_apply_stamps_demotions(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        sidecar = tmp_path / "skills" / ".usage.json"
        sidecar.parent.mkdir(parents=True, exist_ok=True)
        sidecar.write_text(
            json.dumps({
                "main": _rec(use=100, last=_now()),
                "niche": _rec(use=1, last=_now(), description="unrelated topic"),
            }),
            encoding="utf-8",
        )
        assert main(["--apply"]) == 0
        out = capsys.readouterr().out
        assert "demoted 1 skill(s)" in out
        saved = json.loads(sidecar.read_text(encoding="utf-8"))
        assert saved["niche"]["trust_state"] == "demoted"

    def test_json_output(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        assert main(["--json"]) == 0
        data = json.loads(capsys.readouterr().out)
        assert "audited" in data
        assert "verdicts" in data

    def test_help_exits_zero(self, capsys):
        assert main(["--help"]) == 0
        assert "usage" in capsys.readouterr().out


class TestRetrievalPrecision:
    """Actual-use precision over retrieval events (issue #2954, slice 2 of
    #2897): used-when-retrieved rate joined against the curator sidecar,
    with the pool-growth-collapse gate."""

    @staticmethod
    def _events(*retrieved_lists):
        return [
            {"ts": "2026-08-19T00:00:00Z", "query": f"q{i}", "retrieved": list(r)}
            for i, r in enumerate(retrieved_lists)
        ]

    def test_all_retrieved_used_precision_one(self):
        events = self._events(
            ["openai/skills/json-parse"],
            ["json-parse"],  # identifier tail matches usage name
            ["viewed-only"],
        )
        usage = {
            "json-parse": _rec(use=3),
            "viewed-only": _rec(view=5),  # viewed but never used/patched
        }
        rp = retrieval_precision(events, usage)
        assert rp.retrieved_total == 3
        assert rp.used_total == 2  # viewed-only does NOT count as use
        assert rp.precision == 2 / 3
        assert rp.pool_size == 3
        assert rp.gate_triggered is False

    def test_unused_retrieved_precision_collapse_gate(self):
        # 8 identifiers, one used → precision 1/8 < 15% threshold → gate.
        events = self._events(
            ["s/a1", "s/a2", "s/a3", "s/a4", "s/a5"],
            ["s/a6", "s/a7", "s/a8"],
        )
        usage = {"a1": _rec(use=1), **{f"a{i}": _rec() for i in range(2, 9)}}
        rp = retrieval_precision(events, usage)
        assert rp.retrieved_total == 8
        assert rp.used_total == 1
        assert abs(rp.precision - 1 / 8) < 1e-9
        assert rp.pool_size == 8
        assert rp.gate_triggered is True

    def test_insufficient_data_never_gates(self):
        # Empty events and pools below the minimum size never trip the gate.
        assert retrieval_precision([], {}).gate_triggered is False
        small = retrieval_precision(self._events(["s/a1"]), {"a1": _rec()})
        assert small.precision == 0.0
        assert small.pool_size == 1
        assert small.gate_triggered is False

    def test_json_output_includes_precision(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        sidecar = tmp_path / "skills" / "retrieval_events.jsonl"
        sidecar.parent.mkdir(parents=True, exist_ok=True)
        sidecar.write_text(
            '{"ts": "t", "query": "q", "retrieved": ["a/foo"]}\n'
            "this-is-not-json\n"  # corrupt line must be skipped by the loader
            '{"ts": "t", "query": "q", "retrieved": ["b/bar"]}\n',
            encoding="utf-8",
        )
        (tmp_path / "skills" / ".usage.json").write_text(
            json.dumps({"foo": _rec(use=1)}), encoding="utf-8"
        )
        assert main(["--json"]) == 0
        data = json.loads(capsys.readouterr().out)
        rp = data["retrieval_precision"]
        assert rp["events"] == 2
        assert rp["retrieved"] == 2
        assert rp["used"] == 1
        assert abs(rp["precision"] - 0.5) < 1e-9
        assert rp["gate_triggered"] is False


class TestNonStationaryAuditBar:
    """Wiring of the non-stationary audit bar (issue #63) into the daily
    utility audit run — the real call site."""

    def _sidecar(self, tmp_path, usage):
        sidecar = tmp_path / "skills" / ".usage.json"
        sidecar.parent.mkdir(parents=True, exist_ok=True)
        sidecar.write_text(json.dumps(usage), encoding="utf-8")
        return sidecar

    def test_first_run_accepts_baseline_then_carries_traps(
        self, tmp_path, monkeypatch, capsys
    ):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        self._sidecar(
            tmp_path,
            {
                "main": _rec(use=100, last=_now()),
                "tiny": _rec(use=1, last=_now(), description="unrelated topic"),
            },
        )
        assert main([]) == 0
        out1 = capsys.readouterr().out
        assert "[utility-audit][bar]" in out1
        assert "first run" in out1
        state_file = tmp_path / "evolution" / "audit-bar-state.json"
        assert state_file.exists()
        saved = json.loads(state_file.read_text(encoding="utf-8"))
        assert len(saved["accepted_observations"]) == 2
        assert saved["miss_threshold"] == 2

        # Second run: the accepted observations are carried forward as
        # calibration traps.
        assert main([]) == 0
        out2 = capsys.readouterr().out
        assert "calibration traps carried forward" in out2
        assert "known/accepted" in out2

    def test_drift_against_accepted_baseline_is_reported(
        self, tmp_path, monkeypatch, capsys
    ):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        self._sidecar(
            tmp_path,
            {
                "main": _rec(use=100, last=_now()),
                "tiny": _rec(use=1, last=_now(), description="unrelated topic"),
            },
        )
        assert main([]) == 0
        capsys.readouterr().out

        # "main" collapses to inert (keep -> remove): drift. "tiny" vanishes:
        # disappearance drift. "giant" appears: new drift until accepted.
        self._sidecar(
            tmp_path,
            {"main": _rec(), "giant": _rec(use=1000, last=_now())},
        )
        assert main([]) == 0
        out = capsys.readouterr().out
        assert "NEW DRIFT: main" in out
        assert "disappeared" in out
        assert "NEW DRIFT: giant" in out

    def test_bar_prompt_prints_rubric_and_traps(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        self._sidecar(tmp_path, {"main": _rec(use=100, last=_now())})
        assert main([]) == 0
        capsys.readouterr().out
        assert main(["--bar-prompt"]) == 0
        out = capsys.readouterr().out
        assert "NON-STATIONARY AUDIT BAR" in out
        assert "ACTIVE AUDIT RUBRIC" in out
        assert "CALIBRATION TRAPS" in out
        assert "KNOWN/ACCEPTED: skill:main" in out
        assert "never report" in out

    def test_json_includes_audit_bar_state(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        self._sidecar(tmp_path, {"main": _rec(use=100, last=_now())})
        assert main(["--json"]) == 0
        data = json.loads(capsys.readouterr().out)
        assert data["audit_bar"] is not None
        assert data["audit_bar"]["rubric_variant"] == 0
        assert data["audit_bar"]["miss_threshold"] == 2
        assert data["audit_bar"]["calibration_traps"] == 1

    def test_miss_threshold_flag_is_configurable(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        self._sidecar(tmp_path, {"main": _rec(use=100, last=_now())})
        assert main(["--json", "--miss-threshold", "3"]) == 0
        data = json.loads(capsys.readouterr().out)
        assert data["audit_bar"]["miss_threshold"] == 3
        saved = json.loads(
            (tmp_path / "evolution" / "audit-bar-state.json").read_text(
                encoding="utf-8"
            )
        )
        assert saved["miss_threshold"] == 3
