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
