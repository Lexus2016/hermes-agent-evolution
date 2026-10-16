"""Implementation of hermes evolution CLI commands (issue #3064)."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
from typing import Optional

from evolution.lib.ai4ai_benchmark import (
    BUILTIN_TASKS,
    BenchmarkRunResult,
    compute_trend,
    get_metrics_path,
    load_benchmark_history,
    record_benchmark_run,
    run_benchmark_suite,
)


def _get_current_commit_sha() -> Optional[str]:
    try:
        res = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        )
        return res.stdout.strip()
    except Exception:
        return None


def cmd_evolution(args) -> None:
    subcmd = getattr(args, "evolution_command", None) or "benchmark"

    if subcmd == "benchmark":
        action = getattr(args, "benchmark_action", None) or "run"

        if action == "run":
            commit = _get_current_commit_sha()
            metrics_path = Path(args.metrics_path) if getattr(args, "metrics_path", None) else None
            stage_filter = getattr(args, "stage", None)
            tasks = [t for t in BUILTIN_TASKS if t.stage == stage_filter] if stage_filter else BUILTIN_TASKS

            result = run_benchmark_suite(tasks=tasks, commit_sha=commit)

            if getattr(args, "record", True):
                saved_path = record_benchmark_run(result, metrics_path=metrics_path)
            else:
                saved_path = None

            if getattr(args, "json", False):
                out = result.to_dict()
                if saved_path:
                    out["recorded_to"] = str(saved_path)
                print(json.dumps(out, indent=2, ensure_ascii=False))
                return

            print("═══ AI4AI-Bench Recursive Self-Improvement Benchmark ═══")
            print(f"Run ID: {result.run_id} | Commit: {result.commit_sha or 'unknown'}")
            print(
                f"Composite RSI Score: {result.composite_rsi_score * 100:.1f}% "
                f"(Pass Rate: {result.pass_rate * 100:.1f}%, {result.passed_tasks}/{result.total_tasks} tasks)"
            )
            print()
            print("📊 Stage Breakdown:")
            for stg, sc in result.stage_scores.items():
                print(f"  • {stg.capitalize():<12}: {sc * 100:.1f}%")
            print()
            print("📋 Task Results:")
            for tr in result.task_results:
                icon = "✓" if tr.passed else "✗"
                print(f"  [{icon}] {tr.task_id} ({tr.stage}): score={tr.score:.2f} ({tr.duration_ms}ms)")
                if tr.error:
                    print(f"      Error: {tr.error}")
            if saved_path:
                print()
                print(f"✓ Recorded metrics to {saved_path}")

        elif action == "trend":
            days = getattr(args, "days", 7) or 7
            metrics_path = Path(args.metrics_path) if getattr(args, "metrics_path", None) else None
            history = load_benchmark_history(days=days, metrics_path=metrics_path)
            trend = compute_trend(history)

            if getattr(args, "json", False):
                print(json.dumps(trend, indent=2, ensure_ascii=False))
                return

            print(f"═══ AI4AI-Bench RSI Trend ({days}-Day Lookback) ═══")
            print(f"Historical Runs: {trend['runs_count']}")
            if trend["runs_count"] == 0:
                print("No benchmark runs recorded in this window. Run 'hermes evolution benchmark run' to start tracking.")
                return

            print(f"Current RSI Score : {trend['current_rsi_score'] * 100:.1f}%")
            print(f"Baseline RSI Score: {trend['baseline_rsi_score'] * 100:.1f}%")
            print(f"Average RSI Score : {trend['average_rsi_score'] * 100:.1f}%")
            print(f"Trajectory Trend  : {trend['rsi_trend'].upper()} (delta: {trend['delta']:+.2%})")

            if trend.get("regression_detected"):
                print("⚠️  REGRESSION WARNING: Significant RSI degradation detected (>5% drop)!")

            print()
            print("📈 Stage Averages:")
            for stg, val in trend.get("stage_averages", {}).items():
                print(f"  • {stg.capitalize():<12}: {val * 100:.1f}%")

        elif action == "tasks":
            from dataclasses import asdict
            if getattr(args, "json", False):
                print(json.dumps([asdict(t) for t in BUILTIN_TASKS], indent=2, ensure_ascii=False))
                return

            print(f"═══ AI4AI-Bench Held-Out Task Suite ({len(BUILTIN_TASKS)} tasks) ═══")
            for t in BUILTIN_TASKS:
                print(f"• [{t.stage.upper()}] {t.task_id}")
                print(f"  Title: {t.title}")
                print(f"  Target Component: {t.target_component}")
                print(f"  Description: {t.description}")
                if t.invariants:
                    print(f"  Invariants: {', '.join(t.invariants)}")
                print()

        else:
            print(f"unknown benchmark action: {action}", file=sys.stderr)
            sys.exit(2)

    else:
        print(f"unknown evolution subcommand: {subcmd}", file=sys.stderr)
        sys.exit(2)
