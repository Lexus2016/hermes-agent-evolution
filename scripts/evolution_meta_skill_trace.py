#!/usr/bin/env python3
"""Meta-skill variant tracking — Phase 1 instrumentation (#1876, child of #1872).

Records per-cycle which analysis-prompt/procedure variant was used and what
downstream skill-quality delta resulted.  This is the data the slow meta-skill
optimization loop (MetaSkill-Evolve, arXiv:2607.05297) needs before any
meta-skill optimization can happen.  Phase 1 only collects signal.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

DEFAULT_VARIANT_ID = "default-v1"


def _evolution_dir() -> Path:
    env = os.environ.get("EVOLUTION_PROFILE_DIR", "").strip()
    if env:
        return Path(env)
    hh = os.environ.get("HERMES_HOME", "").strip()
    return Path(hh) / "evolution" if hh else Path.home() / ".hermes" / "evolution"


def trace_path(evolution_dir: Optional[Path] = None) -> Path:
    return (evolution_dir or _evolution_dir()) / "meta-skill-traces.jsonl"


@dataclass
class MetaSkillTrace:
    """One per-cycle variant-outcome record."""

    date: str = ""
    variant_id: str = DEFAULT_VARIANT_ID
    selected: int = 0
    merged: int = 0
    selected_issue_ids: List[int] = field(default_factory=list)
    merged_issue_ids: List[int] = field(default_factory=list)
    skills_created: int = 0
    skills_merged: int = 0
    realized_impact: str = ""
    effort_budget: float = 3.0
    timestamp: float = 0.0

    def __post_init__(self) -> None:
        if not self.timestamp:
            self.timestamp = time.time()

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "MetaSkillTrace":
        return cls(**{
            k: d.get(k, cls.__dataclass_fields__[k].default)
            for k in d
            if k in cls.__dataclass_fields__
        })


def append_trace(trace: MetaSkillTrace, evolution_dir: Optional[Path] = None) -> Path:
    """Append a trace record to ``meta-skill-traces.jsonl``."""
    p = trace_path(evolution_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps(asdict(trace), separators=(",", ":")) + "\n")
    return p


def load_traces(evolution_dir: Optional[Path] = None) -> List[MetaSkillTrace]:
    """Load all traces. Missing file → empty list."""
    p = trace_path(evolution_dir)
    if not p.exists():
        return []
    traces: List[MetaSkillTrace] = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
            if isinstance(d, dict):
                traces.append(MetaSkillTrace.from_dict(d))
        except (json.JSONDecodeError, TypeError):
            continue
    return traces


def variant_summary(
    traces: List[MetaSkillTrace], min_cycles: int = 3
) -> Dict[str, Dict[str, Any]]:
    """Aggregate per-variant outcome metrics for the slow loop."""
    by_variant: Dict[str, List[MetaSkillTrace]] = {}
    for t in traces:
        by_variant.setdefault(t.variant_id, []).append(t)
    summary: Dict[str, Dict[str, Any]] = {}
    for vid, records in sorted(by_variant.items()):
        sel = sum(r.selected for r in records)
        mrg = sum(r.merged for r in records)
        summary[vid] = {
            "cycles": len(records),
            "total_selected": sel,
            "total_merged": mrg,
            "merge_rate": round(mrg / sel, 3) if sel else 0.0,
            "insufficient_data": len(records) < min_cycles,
        }
    return summary
