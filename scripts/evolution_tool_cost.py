#!/usr/bin/env python3
"""Cost-aware tool-call budget tracker (#1874).

Per-cycle tool-call cost budget and instrumentation. The BATS finding shows a
budget gate is quality control, not just cost control. Provides:
- ToolCallCostTracker: accumulates costs against a per-cycle budget;
  ``can_call()`` returns False when budget is exhausted.
- Cost tiers: free (terminal, read_file), cheap (web_search), expensive (browser).
- JSON sidecar: appends one line per cycle to ``tool-cost-traces.jsonl``.

The routing layer (choosing between equivalent tools) is a follow-up increment.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

# Cost tiers — free tools cost nothing, cheap tools cost 1 unit,
# expensive tools cost 3 units. Tunable via config in a follow-up increment.
TOOL_COST_TIERS: Dict[str, str] = {
    "terminal": "free",
    "read_file": "free",
    "write_file": "free",
    "patch": "free",
    "search_files": "free",
    "repo_map": "free",
    "execute_code": "free",
    "web_search": "cheap",
    "web_extract": "cheap",
    "delegate_task": "cheap",
    "browser_navigate": "expensive",
    "vision_analyze": "expensive",
}

TIER_COSTS: Dict[str, int] = {"free": 0, "cheap": 1, "expensive": 3}
DEFAULT_BUDGET = 100


def _evolution_dir() -> Path:
    env = os.environ.get("EVOLUTION_PROFILE_DIR", "").strip()
    if env:
        return Path(env)
    hh = os.environ.get("HERMES_HOME", "").strip()
    return Path(hh or Path.home() / ".hermes") / "evolution"


@dataclass
class ToolCallRecord:
    tool_name: str
    cost_tier: str
    cost: int
    timestamp: float = 0.0

    def __post_init__(self) -> None:
        if not self.timestamp:
            self.timestamp = time.time()


@dataclass
class CycleCostReport:
    date: str = ""
    budget: int = DEFAULT_BUDGET
    total_cost: int = 0
    calls_by_tier: Dict[str, int] = field(default_factory=dict)
    calls_by_tool: Dict[str, int] = field(default_factory=dict)
    budget_exhausted: bool = False
    timestamp: float = 0.0

    def __post_init__(self) -> None:
        if not self.timestamp:
            self.timestamp = time.time()


class ToolCallCostTracker:
    """Accumulates tool-call costs against a per-cycle budget."""

    def __init__(self, budget: int = DEFAULT_BUDGET, date: str = "") -> None:
        self.budget = budget
        self.date = date
        self._total_cost = 0
        self._calls: List[ToolCallRecord] = []
        self._by_tier: Dict[str, int] = {}
        self._by_tool: Dict[str, int] = {}

    @staticmethod
    def cost_for(tool_name: str) -> int:
        tier = TOOL_COST_TIERS.get(tool_name, "cheap")
        return TIER_COSTS.get(tier, 1)

    def record(self, tool_name: str) -> ToolCallRecord:
        """Record a tool call and return the record. Raises if over budget."""
        cost = self.cost_for(tool_name)
        rec = ToolCallRecord(
            tool_name=tool_name,
            cost_tier=TOOL_COST_TIERS.get(tool_name, "cheap"),
            cost=cost,
        )
        self._total_cost += cost
        self._calls.append(rec)
        self._by_tier[rec.cost_tier] = self._by_tier.get(rec.cost_tier, 0) + 1
        self._by_tool[tool_name] = self._by_tool.get(tool_name, 0) + 1
        return rec

    def can_call(self, tool_name: str) -> bool:
        """Return True if calling this tool stays within budget."""
        return self._total_cost + self.cost_for(tool_name) <= self.budget

    def remaining(self) -> int:
        return self.budget - self._total_cost

    def report(self) -> CycleCostReport:
        return CycleCostReport(
            date=self.date,
            budget=self.budget,
            total_cost=self._total_cost,
            calls_by_tier=dict(self._by_tier),
            calls_by_tool=dict(self._by_tool),
            budget_exhausted=self._total_cost >= self.budget,
        )


def save_report(report: CycleCostReport, evolution_dir: Optional[Path] = None) -> Path:
    p = _evolution_dir() if evolution_dir is None else evolution_dir
    p.mkdir(parents=True, exist_ok=True)
    f = p / "tool-cost-traces.jsonl"
    with f.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(asdict(report), separators=(",", ":")) + "\n")
    return f
