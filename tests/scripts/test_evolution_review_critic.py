"""Tests for the adversarial reviewer-to-critic gate (#92).

The critic pass runs AFTER the reviewer approves a diff: it must either
articulate a concrete, evidence-grounded failure mode (block → rework) or
explicitly agree (proceed). Un-grounded dissent is treated as noise and does
not block. Verdicts are recorded into the stage-gate ledger.
"""

import io
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
from evolution_qc_review import (  # fmt: skip
    adjudicate_review,
    build_critic_task,
    main,
    parse_critic_report,
    record_critic_verdict,
)

_j = json.dumps


def _critic(dissent, failure_mode="", agreement_reason="", grounded=None):
    if grounded is None:
        grounded = bool(dissent and failure_mode)
    return {
        "dissent": dissent,
        "grounded": grounded,
        "failure_mode": failure_mode,
        "agreement_reason": agreement_reason,
    }


def _reviewer(ok=True):
    return {"ok": ok, "verdict": "pass" if ok else "fail", "has_blocking": not ok}


class TestBuildCriticTask:
    def test_build_task(self):
        task = build_critic_task("Implemented X", ["a.py", "b.py"], issue_number=92)
        assert task["role"] == "leaf"
        assert "Implemented X" in task["goal"]
        assert "a.py" in task["goal"]
        assert "#92" in task["context"]
        assert "file" in task["toolsets"]

    def test_build_no_files(self):
        assert "no files listed" in build_critic_task("s", [])["goal"]

    def test_goal_is_adversarial(self):
        goal = build_critic_task("s", [])["goal"]
        assert "critic" in goal.lower()
        assert "prove it WRONG" in goal


class TestParseCriticReport:
    def test_grounded_dissent(self):
        raw = _j({
            "dissent": True,
            "grounded": True,
            "failure_mode": "drops messages on retry",
        })
        r = parse_critic_report(raw)
        assert r["dissent"] and r["grounded"] and r["ok"]
        assert "drops messages" in r["failure_mode"]

    def test_explicit_agreement(self):
        raw = _j({"dissent": False, "agreement_reason": "no failure mode found"})
        r = parse_critic_report(raw)
        assert not r["dissent"] and r["ok"]

    def test_ungrounded_dissent_is_noise(self):
        # dissent without a concrete failure_mode is not a valid objection
        raw = _j({"dissent": True, "failure_mode": ""})
        r = parse_critic_report(raw)
        assert r["dissent"] and not r["grounded"] and not r["ok"]

    def test_garbage(self):
        for bad in ("", "no json", None):
            assert not parse_critic_report(bad)["ok"]


class TestAdjudicateReview:
    def test_reviewer_fail_blocks(self):
        v = adjudicate_review(_reviewer(ok=False), _critic(False))
        assert v["block"] and v["verdict"] == "rework"

    def test_known_flaw_diff_caught_by_critic(self):
        # regression fixture: reviewer approved, critic found a grounded flaw
        critic = _critic(True, failure_mode="unbounded retry loop under SIGINT")
        v = adjudicate_review(_reviewer(ok=True), critic)
        assert v["block"] and v["verdict"] == "rework"
        assert "retry loop" in v["reason"]

    def test_clean_diff_not_blocked(self):
        critic = _critic(False, agreement_reason="nothing to object to")
        v = adjudicate_review(_reviewer(ok=True), critic)
        assert not v["block"] and v["verdict"] == "proceed"

    def test_ungrounded_dissent_does_not_block(self):
        # dissent without a grounded failure mode is noise → proceed
        v = adjudicate_review(_reviewer(ok=True), _critic(True, grounded=False))
        assert not v["block"] and v["verdict"] == "proceed"

    def test_critic_disabled_proceeds(self):
        v = adjudicate_review(
            _reviewer(ok=True), _critic(True, "flaw"), critic_enabled=False
        )
        assert not v["block"]


class TestRecordCriticVerdict:
    def test_writes_ledger_record(self, tmp_path):
        ledger = tmp_path / "stage_gate.jsonl"
        verdict = {"block": True, "verdict": "rework", "reason": "x"}
        record_critic_verdict(
            str(ledger),
            verdict,
            reviewer_report=_reviewer(ok=True),
            critic_report=_critic(True, "concrete flaw"),
        )
        lines = ledger.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 1
        rec = json.loads(lines[0])
        assert rec["kind"] == "review_critic"
        assert rec["verdict"] == "rework"
        assert rec["reviewer_ok"] is True
        assert rec["dissent"] is True and rec["grounded"] is True


class TestCli:
    def test_cli_build_critic(self, capsys):
        rc = main([
            "x",
            "build-critic",
            "--summary",
            "impl X",
            "--files",
            "a.py",
            "--issue",
            "92",
        ])
        assert rc == 0
        out = json.loads(capsys.readouterr().out)
        assert "impl X" in out["goal"] and out["role"] == "leaf"
        assert main(["x", "build-critic", "--files", "a.py"]) == 2  # missing --summary

    def test_cli_parse_critic(self, capsys, monkeypatch):
        monkeypatch.setattr(
            sys, "stdin", io.StringIO(_j({"dissent": False, "agreement_reason": "ok"}))
        )
        assert main(["x", "parse-critic"]) == 0
        assert json.loads(capsys.readouterr().out)["ok"]
        monkeypatch.setattr(
            sys, "stdin", io.StringIO(_j({"dissent": True, "failure_mode": ""}))
        )
        assert main(["x", "parse-critic"]) == 1

    def test_cli_adjudicate(self, tmp_path, capsys):
        reviewer = tmp_path / "reviewer.json"
        critic = tmp_path / "critic.json"
        reviewer.write_text(
            _j({"verdict": "pass", "has_blocking": False}), encoding="utf-8"
        )
        critic.write_text(
            _j({"dissent": True, "grounded": True, "failure_mode": "known flaw"}),
            encoding="utf-8",
        )
        rc = main([
            "x",
            "adjudicate",
            "--reviewer",
            str(reviewer),
            "--critic",
            str(critic),
        ])
        assert rc == 1  # blocked → rework
        assert json.loads(capsys.readouterr().out)["block"] is True

    def test_cli_adjudicate_ledger(self, tmp_path, capsys):
        reviewer = tmp_path / "reviewer.json"
        critic = tmp_path / "critic.json"
        ledger = tmp_path / "ledger.jsonl"
        reviewer.write_text(
            _j({"verdict": "pass", "has_blocking": False}), encoding="utf-8"
        )
        critic.write_text(
            _j({"dissent": False, "agreement_reason": "fine"}), encoding="utf-8"
        )
        rc = main([
            "x",
            "adjudicate",
            "--reviewer",
            str(reviewer),
            "--critic",
            str(critic),
            "--ledger",
            str(ledger),
        ])
        assert rc == 0
        assert json.loads(ledger.read_text(encoding="utf-8"))["verdict"] == "proceed"
