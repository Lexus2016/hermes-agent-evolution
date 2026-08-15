# -*- coding: utf-8 -*-
"""Causal discovery and topology optimization for Hydra orchestrator dispatch (issue #2438).

Applies causal discovery over multi-agent dispatch communication topologies (arXiv cs.MA
2026-08-13):
1. Instruments dispatch events (trigger signal -> stage dispatched -> artifacts -> outcome).
2. Performs offline causal analysis ranking edges by downstream effect.
3. Identifies no-effect edges for pruning (eliminating stale re-wake loops) and
   independent co-occurring stages for batching.
4. Computes noop-tick rates and produces actionable prune/batch reports.
"""

from __future__ import annotations

import json
import math
import time
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

__all__ = [
    "DispatchEvent",
    "DispatchEdge",
    "TopologyAnalysisReport",
    "DispatchTopologyAnalyzer",
]


@dataclass
class DispatchEvent:
    """A single stage dispatch event in the orchestrator pipeline."""

    tick: int
    trigger_signal: str
    stage: str
    artifacts_read: List[str] = field(default_factory=list)
    artifacts_written: List[str] = field(default_factory=list)
    outcome: str = "real"  # "noop", "real", "blocked"
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> DispatchEvent:
        return cls(
            tick=int(d.get("tick", 0)),
            trigger_signal=str(d.get("trigger_signal", "")),
            stage=str(d.get("stage", "")),
            artifacts_read=list(d.get("artifacts_read", []) or []),
            artifacts_written=list(d.get("artifacts_written", []) or []),
            outcome=str(d.get("outcome", "real")),
            timestamp=float(d.get("timestamp", 0.0)),
        )


@dataclass
class DispatchEdge:
    """A communication/dispatch edge between a trigger signal and a pipeline stage."""

    source_signal: str
    target_stage: str
    total_fires: int = 0
    real_outcomes: int = 0
    noop_outcomes: int = 0
    blocked_outcomes: int = 0
    causal_effect_score: float = 0.0

    def compute_score(self) -> float:
        """Compute the empirical causal effect score (rate of non-noop outcomes)."""
        if self.total_fires == 0:
            self.causal_effect_score = 0.0
            return 0.0
        # Positive effect when stage produces real or necessary blocking outcome
        self.causal_effect_score = round(self.real_outcomes / self.total_fires, 4)
        return self.causal_effect_score

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> DispatchEdge:
        return cls(
            source_signal=str(d.get("source_signal", "")),
            target_stage=str(d.get("target_stage", "")),
            total_fires=int(d.get("total_fires", 0)),
            real_outcomes=int(d.get("real_outcomes", 0)),
            noop_outcomes=int(d.get("noop_outcomes", 0)),
            blocked_outcomes=int(d.get("blocked_outcomes", 0)),
            causal_effect_score=float(d.get("causal_effect_score", 0.0)),
        )


@dataclass
class TopologyAnalysisReport:
    """Comprehensive summary of causal topology analysis."""

    total_ticks: int
    total_events: int
    noop_rate: float
    edges: List[DispatchEdge]
    prune_candidates: List[DispatchEdge]
    batch_candidates: List[List[str]]

    def summary_dict(self) -> Dict[str, Any]:
        return {
            "total_ticks": self.total_ticks,
            "total_events": self.total_events,
            "noop_rate": self.noop_rate,
            "edge_count": len(self.edges),
            "prune_candidates": [e.to_dict() for e in self.prune_candidates],
            "batch_candidates": self.batch_candidates,
        }


class DispatchTopologyAnalyzer:
    """Analyzes dispatch history to discover causal structure, prune stale edges, and batch independent stages."""

    def __init__(self) -> None:
        self.events: List[DispatchEvent] = []

    def record_event(self, event: DispatchEvent) -> None:
        """Record a single dispatch event."""
        self.events.append(event)

    def ingest_events(self, events: Sequence[DispatchEvent]) -> None:
        """Bulk ingest events."""
        self.events.extend(events)

    def ingest_from_jsonl(self, jsonl_path: str | Path) -> int:
        """Load events from a JSONL log file."""
        p = Path(jsonl_path)
        if not p.exists():
            return 0
        count = 0
        with open(p, "r", encoding="utf-8") as f:
            for line in f:
                line_str = line.strip()
                if line_str:
                    try:
                        data = json.loads(line_str)
                        self.events.append(DispatchEvent.from_dict(data))
                        count += 1
                    except Exception:
                        continue
        return count

    def compute_noop_rate(self) -> float:
        """Calculate overall fraction of noop dispatch events."""
        if not self.events:
            return 0.0
        noop_count = sum(1 for e in self.events if e.outcome == "noop")
        return round(noop_count / len(self.events), 4)

    def analyze_causal_topology(
        self,
        min_effect_threshold: float = 0.15,
        min_sample_size: int = 4,
        co_occurrence_threshold: float = 0.75,
    ) -> TopologyAnalysisReport:
        """Discover edges, rank causal effects, and identify prune and batch recommendations."""
        edge_map: Dict[Tuple[str, str], DispatchEdge] = {}
        tick_to_stages: Dict[int, Set[str]] = defaultdict(set)
        ticks_seen: Set[int] = set()

        for ev in self.events:
            ticks_seen.add(ev.tick)
            tick_to_stages[ev.tick].add(ev.stage)
            key = (ev.trigger_signal, ev.stage)
            if key not in edge_map:
                edge_map[key] = DispatchEdge(
                    source_signal=ev.trigger_signal,
                    target_stage=ev.stage,
                )
            edge = edge_map[key]
            edge.total_fires += 1
            if ev.outcome == "real":
                edge.real_outcomes += 1
            elif ev.outcome == "noop":
                edge.noop_outcomes += 1
            elif ev.outcome == "blocked":
                edge.blocked_outcomes += 1

        # Calculate scores
        all_edges = list(edge_map.values())
        for edge in all_edges:
            edge.compute_score()

        # Sort edges by causal effect score descending
        all_edges.sort(
            key=lambda e: (e.causal_effect_score, e.total_fires), reverse=True
        )

        # 1. Prune candidates: high sample size with causal effect below threshold (stale re-wakes)
        prune_candidates = [
            e
            for e in all_edges
            if e.total_fires >= min_sample_size
            and e.causal_effect_score < min_effect_threshold
        ]

        # 2. Batch candidates: pairs of stages that consistently co-occur across ticks and have disjoint written artifacts
        stage_co_counts: Dict[Tuple[str, str], int] = defaultdict(int)
        stage_total_ticks: Dict[str, int] = defaultdict(int)

        for tick, stages in tick_to_stages.items():
            for s in stages:
                stage_total_ticks[s] += 1
            stages_list = sorted(list(stages))
            for i in range(len(stages_list)):
                for j in range(i + 1, len(stages_list)):
                    stage_co_counts[(stages_list[i], stages_list[j])] += 1

        batch_candidates: List[List[str]] = []
        for (s1, s2), co_count in stage_co_counts.items():
            total = min(stage_total_ticks[s1], stage_total_ticks[s2])
            if total >= min_sample_size:
                co_rate = co_count / total
                if co_rate >= co_occurrence_threshold:
                    batch_candidates.append([s1, s2])

        return TopologyAnalysisReport(
            total_ticks=len(ticks_seen),
            total_events=len(self.events),
            noop_rate=self.compute_noop_rate(),
            edges=all_edges,
            prune_candidates=prune_candidates,
            batch_candidates=batch_candidates,
        )
