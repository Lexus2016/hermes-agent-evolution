"""Tests for cost-aware tool-call budget tracker (#1874)."""

import json

from scripts.evolution_tool_cost import (
    CycleCostReport,
    ToolCallCostTracker,
    save_report,
)


def test_free_tools_cost_zero():
    t = ToolCallCostTracker(budget=10, date="d1")
    assert t.cost_for("terminal") == 0
    assert t.cost_for("read_file") == 0
    assert t.cost_for("search_files") == 0


def test_cheap_and_expensive_tiers():
    t = ToolCallCostTracker(budget=10, date="d1")
    assert t.cost_for("web_search") == 1
    assert t.cost_for("browser_navigate") == 3
    assert t.cost_for("some_custom_tool") == 1  # unknown defaults cheap


def test_record_and_can_call():
    t = ToolCallCostTracker(budget=4, date="d1")
    t.record("terminal")
    assert t.remaining() == 4
    t.record("web_search")
    assert t.remaining() == 3
    assert t.can_call("browser_navigate")  # 3 <= 3
    t.record("browser_navigate")
    assert not t.can_call("browser_navigate")  # 3+3=6 > 4
    assert t.can_call("terminal")  # free
    r = t.report()
    assert r.budget_exhausted is True  # 0+1+3=4 >= 4
    assert r.calls_by_tier.get("cheap") == 1
    assert r.calls_by_tier.get("expensive") == 1
    assert r.calls_by_tool.get("web_search") == 1


def test_save_report_writes_jsonl(tmp_path):
    r = CycleCostReport(date="d1", budget=10, total_cost=5)
    p = save_report(r, evolution_dir=tmp_path)
    assert p.exists()
    d = json.loads(p.read_text().strip())
    assert d["date"] == "d1"
    assert d["budget"] == 10
    assert d["total_cost"] == 5
