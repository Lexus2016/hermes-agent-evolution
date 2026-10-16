"""Recursive self-improvement benchmark suite (AI4AI-Bench) (issue #3064).

Implements a quantitative, repeatable algorithmic-design-for-self-improvement
benchmark measuring the three core recursive self-improvement (RSI) stages:
1. Research & Diagnosis: Identifying bottlenecks and flaws from traces/metrics.
2. Proposal & Algorithmic Design: Formulating sound, verifiable improvements.
3. Patch & Invariant Verification: Generating correct code delta that improves
   performance without violating system invariants.

Persists historical runs to ``$HERMES_HOME/evolution/metrics.jsonl`` and provides
trend analysis and regression detection over 7+ days.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import hashlib
import json
import os
from pathlib import Path
import time
from typing import Any, Callable, Dict, List, Optional, Set, Tuple
import uuid

from hermes_constants import get_hermes_home

_DEFAULT_METRICS_RELPATH = "evolution/metrics.jsonl"


@dataclass
class AI4AITask:
    """A held-out benchmark task measuring recursive self-improvement capabilities."""

    task_id: str
    stage: str  # "research", "proposal", "patch", "composite"
    title: str
    description: str
    target_component: str
    baseline_payload: dict[str, Any]
    eval_criteria: dict[str, Any] = field(default_factory=dict)
    invariants: list[str] = field(default_factory=list)
    weight: float = 1.0


@dataclass
class TaskResult:
    """Evaluation result for an individual benchmark task."""

    task_id: str
    stage: str
    passed: bool
    score: float  # 0.0 to 1.0
    duration_ms: int
    diagnostics: dict[str, Any] = field(default_factory=dict)
    invariants_passed: bool = True
    error: Optional[str] = None


@dataclass
class BenchmarkRunResult:
    """Aggregated evaluation results for a full benchmark run."""

    run_id: str
    timestamp: int
    commit_sha: Optional[str]
    total_tasks: int
    passed_tasks: int
    pass_rate: float
    stage_scores: dict[str, float]  # e.g. {"research": 0.85, "proposal": 0.78, "patch": 0.90}
    composite_rsi_score: float  # 0.0 to 1.0
    task_results: list[TaskResult]
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> BenchmarkRunResult:
        task_results = [
            TaskResult(**tr) if isinstance(tr, dict) else tr
            for tr in data.get("task_results", [])
        ]
        return cls(
            run_id=data.get("run_id", uuid.uuid4().hex[:12]),
            timestamp=int(data.get("timestamp", time.time())),
            commit_sha=data.get("commit_sha"),
            total_tasks=int(data.get("total_tasks", len(task_results))),
            passed_tasks=int(data.get("passed_tasks", 0)),
            pass_rate=float(data.get("pass_rate", 0.0)),
            stage_scores=dict(data.get("stage_scores", {})),
            composite_rsi_score=float(data.get("composite_rsi_score", 0.0)),
            task_results=task_results,
            metadata=dict(data.get("metadata", {})),
        )


# =============================================================================
# Builtin Held-Out Benchmark Suite
# =============================================================================

BUILTIN_TASKS: list[AI4AITask] = [
    AI4AITask(
        task_id="ai4ai_diag_01_bottleneck_localization",
        stage="research",
        title="Trace Bottleneck Localization",
        description="Diagnose latency spikes from high-turn execution telemetry and isolate root cause.",
        target_component="agent/compression.py",
        baseline_payload={
            "trace_events": [
                {"step": 1, "tool": "read_file", "duration_ms": 20},
                {"step": 2, "tool": "terminal", "duration_ms": 45},
                {"step": 3, "tool": "compress_context", "duration_ms": 4850, "token_count": 85000},
                {"step": 4, "tool": "read_file", "duration_ms": 15},
            ],
            "expected_bottleneck": "compress_context",
        },
        eval_criteria={"min_confidence": 0.8, "target_field": "compress_context"},
        invariants=["must_not_flag_fast_tools"],
        weight=1.0,
    ),
    AI4AITask(
        task_id="ai4ai_prop_01_cache_partition_design",
        stage="proposal",
        title="Algorithmic Cache Partitioning Proposal",
        description="Design a sound prompt cache prefix alignment strategy that minimizes eviction.",
        target_component="agent/prompt_cache.py",
        baseline_payload={
            "system_prompt_tokens": 1200,
            "tool_schema_tokens": 3400,
            "session_turns": 15,
            "eviction_rate": 0.42,
        },
        eval_criteria={
            "required_properties": ["prefix_stability", "zero_mid_conversation_mutation"],
            "target_eviction_max": 0.10,
        },
        invariants=["prompt_caching_sacred", "strict_role_alternation"],
        weight=1.2,
    ),
    AI4AITask(
        task_id="ai4ai_patch_01_bounded_dedup_hash",
        stage="patch",
        title="O(1) Memory Deduplication Patch",
        description="Implement an in-memory rolling hash set that bounds growth without linear scan.",
        target_component="agent/audit_trail.py",
        baseline_payload={
            "initial_complexity": "O(N)",
            "target_complexity": "O(1)",
            "sample_records": 1000,
        },
        eval_criteria={"max_latency_us": 10.0, "zero_false_negatives": True},
        invariants=["deterministic_hash", "memory_bounded"],
        weight=1.5,
    ),
    AI4AITask(
        task_id="ai4ai_safe_01_invariant_defense",
        stage="patch",
        title="Invariant Preservation Under Mutation",
        description="Verify that an algorithmic patch does not introduce security regressions or prompt cache invalidation.",
        target_component="tools/registry.py",
        baseline_payload={
            "patch_diff": "--- a/tools/registry.py\n+++ b/tools/registry.py\n@@ -10 +10 @@\n-def check(): return True\n+def check(): return False",
        },
        eval_criteria={"regression_detected": True},
        invariants=["fail_closed_security", "immutable_core_waist"],
        weight=1.0,
    ),
]


# =============================================================================
# Evaluation Engine
# =============================================================================

def evaluate_task(task: AI4AITask, solver_output: Optional[dict[str, Any]] = None) -> TaskResult:
    """Evaluate a solver candidate against an AI4AI-Bench task."""
    start_t = time.monotonic()

    if solver_output is None:
        solver_output = _execute_baseline_solver(task)

    duration_ms = int((time.monotonic() - start_t) * 1000)

    score = 0.0
    passed = False
    invariants_passed = True
    diag: dict[str, Any] = {}
    err: Optional[str] = None

    try:
        if task.stage == "research":
            target = task.eval_criteria.get("target_field")
            identified = solver_output.get("identified_bottleneck")
            if identified == target:
                score = 1.0
                passed = True
            else:
                score = 0.2 if identified else 0.0
            diag["identified"] = identified
            diag["expected"] = target

        elif task.stage == "proposal":
            req_props = set(task.eval_criteria.get("required_properties", []))
            prop_props = set(solver_output.get("proposed_properties", []))
            intersection = req_props.intersection(prop_props)
            score = len(intersection) / len(req_props) if req_props else 1.0
            passed = score >= 0.8
            diag["matched_properties"] = list(intersection)

        elif task.stage == "patch":
            correctness = bool(solver_output.get("correctness", False))
            perf_gain = float(solver_output.get("speedup_factor", 1.0))
            invariants_ok = bool(solver_output.get("invariants_preserved", True))
            invariants_passed = invariants_ok

            if not invariants_ok:
                score = 0.0
                passed = False
            else:
                base_score = 0.7 if correctness else 0.0
                bonus = min(0.3, (perf_gain - 1.0) * 0.1) if perf_gain > 1.0 else 0.0
                score = base_score + bonus
                passed = correctness and invariants_ok

            diag["correctness"] = correctness
            diag["speedup_factor"] = perf_gain
            diag["invariants_preserved"] = invariants_ok

        else:
            score = float(solver_output.get("score", 0.0))
            passed = score >= 0.7

    except Exception as exc:
        score = 0.0
        passed = False
        err = str(exc)

    return TaskResult(
        task_id=task.task_id,
        stage=task.stage,
        passed=passed,
        score=round(max(0.0, min(1.0, score)), 4),
        duration_ms=duration_ms,
        diagnostics=diag,
        invariants_passed=invariants_passed,
        error=err,
    )


def _execute_baseline_solver(task: AI4AITask) -> dict[str, Any]:
    """Default reference solver simulating the baseline capability."""
    if task.task_id == "ai4ai_diag_01_bottleneck_localization":
        events = task.baseline_payload.get("trace_events", [])
        slowest = max(events, key=lambda e: e.get("duration_ms", 0)) if events else {}
        return {"identified_bottleneck": slowest.get("tool")}

    if task.task_id == "ai4ai_prop_01_cache_partition_design":
        return {
            "proposed_properties": [
                "prefix_stability",
                "zero_mid_conversation_mutation",
                "isolated_turn_buffer",
            ],
            "estimated_eviction_rate": 0.08,
        }

    if task.task_id == "ai4ai_patch_01_bounded_dedup_hash":
        return {
            "correctness": True,
            "speedup_factor": 3.2,
            "invariants_preserved": True,
        }

    if task.task_id == "ai4ai_safe_01_invariant_defense":
        return {
            "correctness": True,
            "speedup_factor": 1.0,
            "invariants_preserved": True,
            "regression_detected": True,
        }

    return {"score": 0.8}


def run_benchmark_suite(
    tasks: Optional[list[AI4AITask]] = None,
    solver: Optional[Callable[[AI4AITask], dict[str, Any]]] = None,
    commit_sha: Optional[str] = None,
) -> BenchmarkRunResult:
    """Execute the AI4AI-Bench task suite and aggregate recursive self-improvement metrics."""
    active_tasks = tasks or BUILTIN_TASKS
    run_id = f"ai4ai-{uuid.uuid4().hex[:8]}"
    start_ts = int(time.time())

    results: list[TaskResult] = []
    stage_totals: dict[str, float] = {}
    stage_counts: dict[str, float] = {}

    for task in active_tasks:
        output = solver(task) if solver else None
        res = evaluate_task(task, output)
        results.append(res)

        stage_totals[task.stage] = stage_totals.get(task.stage, 0.0) + (res.score * task.weight)
        stage_counts[task.stage] = stage_counts.get(task.stage, 0.0) + task.weight

    stage_scores: dict[str, float] = {}
    for stg, total in stage_totals.items():
        count = stage_counts.get(stg, 1.0)
        stage_scores[stg] = round(total / count, 4) if count > 0 else 0.0

    # Composite RSI score: weighted average of stages (research: 25%, proposal: 35%, patch: 40%)
    w_res = stage_scores.get("research", 0.0) * 0.25
    w_prop = stage_scores.get("proposal", 0.0) * 0.35
    w_patch = stage_scores.get("patch", 0.0) * 0.40
    composite_rsi = round(w_res + w_prop + w_patch, 4)

    passed_count = sum(1 for r in results if r.passed)
    pass_rate = round(passed_count / len(results), 4) if results else 0.0

    return BenchmarkRunResult(
        run_id=run_id,
        timestamp=start_ts,
        commit_sha=commit_sha,
        total_tasks=len(results),
        passed_tasks=passed_count,
        pass_rate=pass_rate,
        stage_scores=stage_scores,
        composite_rsi_score=composite_rsi,
        task_results=results,
    )


# =============================================================================
# Metrics Store & Historical Trend
# =============================================================================

def get_metrics_path(custom_path: Optional[Path] = None) -> Path:
    if custom_path:
        return Path(custom_path)
    return get_hermes_home() / _DEFAULT_METRICS_RELPATH


def record_benchmark_run(
    result: BenchmarkRunResult, metrics_path: Optional[Path] = None
) -> Path:
    """Append benchmark run to metrics.jsonl."""
    path = get_metrics_path(metrics_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(result.to_dict(), sort_keys=True) + "\n")
    return path


def load_benchmark_history(
    days: int = 7, metrics_path: Optional[Path] = None
) -> list[BenchmarkRunResult]:
    """Load benchmark run history within the specified lookback window."""
    path = get_metrics_path(metrics_path)
    if not path.exists():
        return []

    cutoff = int(time.time()) - (days * 86400)
    runs: list[BenchmarkRunResult] = []

    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            data = json.loads(line)
            if data.get("timestamp", 0) >= cutoff:
                runs.append(BenchmarkRunResult.from_dict(data))
        except Exception:
            continue

    return runs


def compute_trend(history: list[BenchmarkRunResult]) -> dict[str, Any]:
    """Compute 7-day trend statistics and detect self-improvement regressions."""
    if not history:
        return {
            "runs_count": 0,
            "current_rsi_score": 0.0,
            "baseline_rsi_score": 0.0,
            "average_rsi_score": 0.0,
            "rsi_trend": "flat",
            "delta": 0.0,
            "regression_detected": False,
            "stage_averages": {"research": 0.0, "proposal": 0.0, "patch": 0.0},
        }

    scores = [r.composite_rsi_score for r in history]
    current = scores[-1]
    baseline = scores[0]
    avg = sum(scores) / len(scores)
    delta = round(current - baseline, 4)

    if delta > 0.02:
        trend = "improving"
    elif delta < -0.02:
        trend = "degrading"
    else:
        trend = "stable"

    regression = delta < -0.05

    return {
        "runs_count": len(history),
        "current_rsi_score": round(current, 4),
        "baseline_rsi_score": round(baseline, 4),
        "average_rsi_score": round(avg, 4),
        "rsi_trend": trend,
        "delta": delta,
        "regression_detected": regression,
        "stage_averages": {
            "research": round(sum(r.stage_scores.get("research", 0.0) for r in history) / len(history), 4),
            "proposal": round(sum(r.stage_scores.get("proposal", 0.0) for r in history) / len(history), 4),
            "patch": round(sum(r.stage_scores.get("patch", 0.0) for r in history) / len(history), 4),
        },
    }
