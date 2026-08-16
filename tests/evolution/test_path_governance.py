# -*- coding: utf-8 -*-
"""Unit tests for path-based runtime governance (#2284)."""

from pathlib import Path

from evolution.lib.path_governance import (
    BLOCK,
    PASS,
    STEER,
    PathEvent,
    PathLog,
    PathPolicyChecker,
    PathVerdict,
    RiskBudget,
)


class TestPathLog:
    def test_record_and_recent(self):
        log = PathLog(max_entries=3)
        log.record("read_file", ".env")
        log.record("web_search", "query")
        log.record("send_message", "hello")
        assert len(log) == 3
        recent = log.recent(2)
        assert [e.tool_name for e in recent] == ["web_search", "send_message"]

    def test_ring_buffer_bounded(self):
        log = PathLog(max_entries=2)
        for i in range(5):
            log.record(f"tool_{i}")
        assert len(log) == 2
        assert log.recent()[0].tool_name == "tool_3"

    def test_disk_append(self, tmp_path: Path):
        log_path = tmp_path / "path.log"
        log = PathLog(log_path=log_path)
        log.record("read_file", ".env")
        assert log_path.exists()
        content = log_path.read_text(encoding="utf-8")
        assert "read_file" in content

    def test_event_serialization(self):
        e = PathEvent(tool_name="read_file", args_summary=".env", result_category="ok")
        d = e.to_dict()
        restored = PathEvent.from_dict(d)
        assert restored.tool_name == "read_file"
        assert restored.args_summary == ".env"


class TestPathPolicyChecker:
    def test_pass_for_benign_path(self):
        log = PathLog()
        log.record("read_file", "schema.sql")
        checker = PathPolicyChecker()
        verdict = checker.evaluate(log, "send_message", "hello")
        assert verdict.verdict == PASS

    def test_block_secrets_read_then_network(self):
        log = PathLog()
        log.record("read_file", "/app/.env")
        checker = PathPolicyChecker(exfil_window=5)
        verdict = checker.evaluate(log, "send_message", "exfil")
        assert verdict.verdict == BLOCK
        assert "exfiltration" in verdict.reason

    def test_steer_skill_write_without_validation(self):
        log = PathLog()
        log.record("read_file", "notes.txt")
        checker = PathPolicyChecker(validation_window=5)
        verdict = checker.evaluate(log, "write_file", "skills/my-skill/SKILL.md")
        assert verdict.verdict == STEER
        assert "run_validation" in verdict.reason

    def test_steer_destructive_op_limit(self):
        log = PathLog()
        for _ in range(3):
            log.record("delete_file", "tmp.txt")
        checker = PathPolicyChecker(destructive_limit=3)
        verdict = checker.evaluate(log, "delete_file", "another.txt")
        assert verdict.verdict == STEER
        assert "destructive" in verdict.reason

    def test_verdict_serialization(self):
        v = PathVerdict(tool_name="send_message", verdict=BLOCK, rule="r", reason="why")
        d = v.to_dict()
        restored = PathVerdict.from_dict(d)
        assert restored.verdict == BLOCK
        assert restored.rule == "r"


class TestRiskBudget:
    def test_pass_costs_nothing(self):
        budget = RiskBudget(budget=10.0)
        budget.record_verdict(PathVerdict(tool_name="t", verdict=PASS))
        assert budget.remaining() == 10.0

    def test_steer_costs_one(self):
        budget = RiskBudget(budget=10.0)
        budget.record_verdict(PathVerdict(tool_name="t", verdict=STEER))
        assert budget.remaining() == 9.0

    def test_block_costs_three(self):
        budget = RiskBudget(budget=10.0)
        budget.record_verdict(PathVerdict(tool_name="t", verdict=BLOCK))
        assert budget.remaining() == 7.0

    def test_budget_exhausted(self):
        budget = RiskBudget(budget=2.0)
        budget.record_verdict(PathVerdict(tool_name="t", verdict=BLOCK))
        assert budget.remaining() == 0.0
        assert budget.exhausted is True
