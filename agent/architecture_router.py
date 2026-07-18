# -*- coding: utf-8 -*-
"""Architecture router: single-agent vs centralized multi-agent selector (#1139).

Classifies each task on two axes — decomposability and tool density (plus
sequentiality) — and recommends the execution architecture, following the
scaling principles from Google Research's "science of scaling agent systems"
(arXiv:2512.08296): parallelizable tasks benefit from centralized multi-agent
coordination; sequential tasks are penalized by multi-agent overhead; and
independent parallel agents amplify errors far more than a centralized
orchestrator.

This slice is a **pre-flight recommender + telemetry**: it computes and logs the
recommended architecture (the calibration dataset the issue asks for) without
changing how the turn actually executes. Acting on the recommendation
(auto-spawning orchestrated workers) is a documented follow-up; keeping this
observation-only makes it safe to ship on by-config without behavior risk.

Config-gated and off by default. Pure functions + frozen dataclasses; no
import-time side effects; standard library only.
"""

from __future__ import annotations

import re
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Deque, Mapping

__all__ = [
    "Architecture",
    "TaskClassification",
    "RoutingDecision",
    "ArchitectureRouterConfig",
    "RouterTelemetry",
    "classify_task",
    "route",
    "maybe_route",
    "load_router_config",
]


class Architecture(str, Enum):
    single_agent = "single_agent"
    centralized_orchestrator = "centralized_orchestrator"
    plan_and_execute = "plan_and_execute"


@dataclass(frozen=True)
class TaskClassification:
    decomposability: float  # 0..1 — can it split into independent sub-questions?
    tool_density: int  # estimated number of distinct tools needed
    sequentiality: float  # 0..1 — do outputs of step N feed step N+1?
    confidence: float  # 0..1 — classifier confidence

    def to_dict(self) -> dict[str, Any]:
        return {
            "decomposability": self.decomposability,
            "tool_density": self.tool_density,
            "sequentiality": self.sequentiality,
            "confidence": self.confidence,
        }


@dataclass(frozen=True)
class RoutingDecision:
    architecture: Architecture
    max_workers: int
    rationale: str
    classification: TaskClassification

    def to_dict(self) -> dict[str, Any]:
        return {
            "architecture": self.architecture.value,
            "max_workers": self.max_workers,
            "rationale": self.rationale,
            "classification": self.classification.to_dict(),
        }


@dataclass(frozen=True)
class ArchitectureRouterConfig:
    enabled: bool = False
    confidence_threshold: float = 0.5  # below this -> safe default (single-agent)
    max_workers_cap: int = 4  # contain error amplification (Google: 17.2x -> 4.4x)
    decomposability_threshold: float = 0.6
    sequentiality_threshold: float = 0.6
    high_tool_density: int = 16  # Google: coordination tax rises past ~16 tools

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any] | None) -> "ArchitectureRouterConfig":
        if not isinstance(data, Mapping):
            return cls()
        d = cls()

        def _f(key: str, default: float) -> float:
            try:
                return float(data.get(key, default))
            except (TypeError, ValueError):
                return default

        def _i(key: str, default: int) -> int:
            try:
                return int(data.get(key, default))
            except (TypeError, ValueError):
                return default

        return cls(
            enabled=bool(data.get("enabled", d.enabled)),
            confidence_threshold=min(1.0, max(0.0, _f("confidence_threshold", d.confidence_threshold))),
            max_workers_cap=max(1, _i("max_workers_cap", d.max_workers_cap)),
            decomposability_threshold=min(1.0, max(0.0, _f("decomposability_threshold", d.decomposability_threshold))),
            sequentiality_threshold=min(1.0, max(0.0, _f("sequentiality_threshold", d.sequentiality_threshold))),
            high_tool_density=max(1, _i("high_tool_density", d.high_tool_density)),
        )


def load_router_config() -> ArchitectureRouterConfig:
    """Lazily load the ``architecture_router`` config section (safe default: off).

    Uses ``load_config_readonly`` (no defensive deepcopy) because this runs once
    per turn in the conversation loop and only reads the config.
    """
    try:
        try:
            from hermes_cli.config import load_config_readonly as _load
        except ImportError:
            from hermes_cli.config import load_config as _load

        cfg = _load()
        if isinstance(cfg, Mapping):
            return ArchitectureRouterConfig.from_mapping(cfg.get("architecture_router", {}))
    except Exception:
        pass
    return ArchitectureRouterConfig()


# Markers used by the heuristic classifier.
_SEQ_MARKERS = (
    "then", "after", "once", "next", "finally", "afterwards", "subsequently",
    "step 1", "step 2", "first,", "second,", "based on the", "using the result",
)
_PARALLEL_MARKERS = (
    "each", "all of", "for every", "respectively", "in parallel", "compare",
    "across", "both", "and also", "as well as", "multiple",
)
_TOOL_MARKERS = (
    "file", "read", "write", "search", "web", "browser", "code", "run",
    "terminal", "download", "api", "database", "image", "video", "email",
    "commit", "test", "screenshot", "fetch", "scrape",
)
_ENUM_RE = re.compile(r"(?:^|\n)\s*(?:\d+[.)]|[-*])\s+")


def _count_markers(text: str, markers: tuple[str, ...]) -> int:
    return sum(1 for m in markers if m in text)


def classify_task(task: str, *, tool_hint_count: int | None = None) -> TaskClassification:
    """Heuristically score a task on decomposability / tool density / sequentiality."""
    text = (task or "").lower()
    if not text.strip():
        return TaskClassification(0.0, 0, 0.0, 0.0)

    enum_items = len(_ENUM_RE.findall(task or ""))
    parallel_hits = _count_markers(text, _PARALLEL_MARKERS)
    seq_hits = _count_markers(text, _SEQ_MARKERS)

    # Decomposability: enumerated items and parallel markers raise it.
    decomposability = min(1.0, 0.2 * enum_items + 0.15 * parallel_hits)
    # Sequentiality: sequential markers raise it; enumerated steps imply order.
    sequentiality = min(1.0, 0.2 * seq_hits + 0.05 * enum_items)
    # Tool density: caller hint wins; else estimate from distinct tool markers.
    if tool_hint_count is not None:
        tool_density = max(0, int(tool_hint_count))
    else:
        distinct = sum(1 for m in _TOOL_MARKERS if m in text)
        tool_density = distinct

    # Confidence grows with the number of signals present.
    signals = enum_items + parallel_hits + seq_hits
    confidence = min(1.0, 0.3 + 0.1 * signals)
    return TaskClassification(
        decomposability=round(decomposability, 3),
        tool_density=tool_density,
        sequentiality=round(sequentiality, 3),
        confidence=round(confidence, 3),
    )


def route(
    classification: TaskClassification,
    config: ArchitectureRouterConfig | None = None,
) -> RoutingDecision:
    """Map a classification to an architecture via the decision table + guardrails."""
    cfg = config or ArchitectureRouterConfig()

    # Guardrail: low confidence -> safe default (single-agent).
    if classification.confidence < cfg.confidence_threshold:
        return RoutingDecision(
            Architecture.single_agent,
            1,
            "low classifier confidence — defaulting to single-agent",
            classification,
        )

    high_decomp = classification.decomposability >= cfg.decomposability_threshold
    high_seq = classification.sequentiality >= cfg.sequentiality_threshold
    high_tools = classification.tool_density >= cfg.high_tool_density

    # Sequential tasks are penalized by multi-agent overhead -> single/plan.
    if high_seq and not high_decomp:
        return RoutingDecision(
            Architecture.plan_and_execute,
            1,
            "sequential task — multi-agent overhead hurts; plan-and-execute",
            classification,
        )

    # Parallelizable tasks -> centralized orchestrator (contains error
    # amplification vs independent parallel agents). Cap workers on high tool
    # density where the coordination tax rises.
    if high_decomp and not high_seq:
        max_workers = min(cfg.max_workers_cap, 3 if high_tools else cfg.max_workers_cap)
        return RoutingDecision(
            Architecture.centralized_orchestrator,
            max_workers,
            "decomposable, low-sequentiality task — centralized orchestrator with capped workers",
            classification,
        )

    # Low decomposability or mixed -> single-agent (the naive-safe default).
    return RoutingDecision(
        Architecture.single_agent,
        1,
        "low decomposability or mixed signals — single-agent",
        classification,
    )


@dataclass
class RouterTelemetry:
    """Bounded in-memory log of routing decisions + outcomes for calibration."""

    capacity: int = 500
    _entries: Deque[dict[str, Any]] = field(default_factory=lambda: deque(maxlen=500))

    def __post_init__(self) -> None:
        self._entries = deque(self._entries, maxlen=max(1, self.capacity))

    def record(self, decision: RoutingDecision, *, outcome: str | None = None) -> None:
        entry = decision.to_dict()
        entry["outcome"] = outcome
        self._entries.append(entry)

    def entries(self) -> list[dict[str, Any]]:
        return list(self._entries)

    def __len__(self) -> int:
        return len(self._entries)


def maybe_route(
    task: str,
    *,
    config: ArchitectureRouterConfig | None = None,
    telemetry: RouterTelemetry | None = None,
    tool_hint_count: int | None = None,
) -> RoutingDecision | None:
    """Runtime seam: classify + route + log a task's recommended architecture.

    Returns ``None`` (and does nothing) unless the router is enabled, so the
    default path is a pure no-op that never changes execution. When enabled it
    returns the recommendation and, if a telemetry sink is given, logs it. Never
    raises.
    """
    cfg = config or load_router_config()
    if not cfg.enabled:
        return None
    try:
        classification = classify_task(task, tool_hint_count=tool_hint_count)
        decision = route(classification, cfg)
        if telemetry is not None:
            telemetry.record(decision)
        return decision
    except Exception:
        return None
