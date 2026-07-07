#!/usr/bin/env python3
"""Evolution funnel metrics — per-cycle aggregate of the pipeline's own output.

Runs as a ``no_agent`` cron job (no LLM). For a given date it reads the structured
stage reports the pipeline already writes and appends ONE JSON line to
``<evolution_dir>/metrics.jsonl`` describing the funnel:

    research proposals -> issues created -> selected -> merged
                       \\-> rejected (by reason)   \\-> skipped

This gives the owner (and evolution-introspection) a measurable view of the
pipeline so it can improve ITSELF — e.g. "reject rate 90% => research-quality
issue", "merged 0 for 3 cycles => integration stuck". Issue #84.

Pure functions + explicit paths so it is import-safe and unit-testable.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Dict

logger = logging.getLogger(__name__)


def _load_json(path: Path) -> Any | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _counts_by(items: list, key: str) -> Dict[str, int]:
    """Count list items by a string field, tolerating missing keys."""
    c: Counter = Counter()
    for it in items if isinstance(items, list) else []:
        if isinstance(it, dict):
            c[str(it.get(key, "unknown"))] += 1
    return dict(c)


def compute_funnel(evolution_dir: Path, date: str) -> Dict[str, Any]:
    """Build the funnel record for one date from whatever stage reports exist.

    Every field defaults to 0 / {} so a missing stage report never crashes the
    metric — it just shows that stage produced nothing recorded that day.
    """

    # Stage reports are normally dicts, but some stages write a bare LIST (e.g.
    # introspection emits a list of patterns). Coerce non-dicts to {} so .get()
    # never crashes the metric — a list-shaped report previously raised
    # AttributeError and killed the whole funnel job (and the realized-impact
    # sidecar refresh that now rides on it).
    def _as_dict(x: Any) -> Dict[str, Any]:
        return x if isinstance(x, dict) else {}

    issues_raw = _load_json(issues_path := evolution_dir / "issues" / f"{date}.json")
    analysis = _as_dict(_load_json(evolution_dir / "analysis" / f"{date}.json"))
    integration = _as_dict(_load_json(evolution_dir / "integration" / f"{date}.json"))
    introspection_raw = _load_json(evolution_dir / "introspection" / f"{date}.json")
    issues = _as_dict(issues_raw)

    selected = analysis.get("selected_for_implementation") or []
    rejected = analysis.get("rejected") or []
    merged = integration.get("merged") or []
    skipped = integration.get("skipped") or []
    # introspection may be a bare list of patterns OR a dict with patterns_found.
    if isinstance(introspection_raw, list):
        patterns = introspection_raw
    else:
        patterns = _as_dict(introspection_raw).get("patterns_found") or []
    # The issues stage has historically emitted several keys for the same list.
    # Canonical is ``issues_created``; aliases are ``created`` and
    # ``proposals_filed`` (used briefly around 2026-07-05). Count any of them so
    # a schema rename never silently zeros the funnel.
    created = (
        issues.get("issues_created")
        or issues.get("created")
        or issues.get("proposals_filed")
        or []
    )
    if "issues_created" not in issues:
        if "created" in issues:
            logger.warning(
                "Schema drift in %s: issues report uses legacy key 'created' "
                "instead of 'issues_created'",
                issues_path,
            )
        elif "proposals_filed" in issues:
            logger.warning(
                "Schema drift in %s: issues report uses legacy key 'proposals_filed' "
                "instead of 'issues_created'",
                issues_path,
            )

    return {
        "date": date,
        # inflow
        "research_proposals": int(issues.get("total_proposals", 0) or 0),
        "proposals_passed_filter": int(issues.get("proposals_passed_filter", 0) or 0),
        "issues_created": len(created) if isinstance(created, list) else 0,
        "introspection_patterns": len(patterns) if isinstance(patterns, list) else 0,
        # triage / selection
        "selected": len(selected) if isinstance(selected, list) else 0,
        "selected_by_reason": _counts_by(selected, "selected_reason"),
        "rejected": len(rejected) if isinstance(rejected, list) else 0,
        "rejected_by_reason": _counts_by(rejected, "reason_code"),
        # outflow
        "merged": len(merged) if isinstance(merged, list) else 0,
        "skipped": len(skipped) if isinstance(skipped, list) else 0,
    }


def append_funnel(metrics_file: Path, record: Dict[str, Any]) -> None:
    """Append one JSON line, idempotently: replace any existing line for the
    same date so re-runs don't duplicate a day."""
    lines = []
    if metrics_file.exists():
        for ln in metrics_file.read_text(encoding="utf-8").splitlines():
            ln = ln.strip()
            if not ln:
                continue
            try:
                obj = json.loads(ln)
            except ValueError:
                continue
            if obj.get("date") != record["date"]:
                lines.append(json.dumps(obj, sort_keys=True))
    lines.append(json.dumps(record, sort_keys=True))
    metrics_file.parent.mkdir(parents=True, exist_ok=True)
    metrics_file.write_text("\n".join(lines) + "\n", encoding="utf-8")


def load_records(metrics_file: Path) -> list[Dict[str, Any]]:
    """Read all funnel records (one JSON object per line), oldest-first,
    skipping blank/malformed lines."""
    out: list[Dict[str, Any]] = []
    if not metrics_file.exists():
        return out
    for ln in metrics_file.read_text(encoding="utf-8").splitlines():
        ln = ln.strip()
        if not ln:
            continue
        try:
            obj = json.loads(ln)
        except ValueError:
            continue
        if isinstance(obj, dict):
            out.append(obj)
    return out


def is_evolution_halted(evolution_dir: Path | None = None) -> bool | None:
    """Check whether the evolution pipeline is in a halted state.

    Returns True if halt-state.txt exists (pipeline has produced zero
    automated deliverables for 5+ consecutive cycles and zero selections
    for 3+ cycles). Returns False if the pipeline is healthy. Returns
    None on any read error (treat as not-halted — fail-open).

    All expensive LLM stages (research, analysis, implementation) should
    call this BEFORE spawning an agent to avoid burning API credits on a
    broken loop (#evolution — halt detection gate).
    """
    if evolution_dir is None:
        evolution_dir = Path(
            os.environ.get(
                "EVOLUTION_PROFILE_DIR",
                str(Path.home() / ".hermes" / "profiles" / "user1" / "evolution"),
            )
        )
    halt_file = evolution_dir / "halt-state.txt"
    try:
        return halt_file.exists()
    except OSError:
        return None


def summarize(records: list[Dict[str, Any]], last: int = 7) -> Dict[str, Any]:
    """Aggregate the last ``last`` funnel records into a signal-quality summary
    that evolution-research reads to self-tune selectivity (#84 feedback loop —
    closes the previously write-only metrics.jsonl).

    reject_rate = rejected / (selected + rejected) over the window: of the issues
    that reached triage, the fraction triage turned down. A high value means
    research is surfacing low-quality proposals, so research should tighten up.
    """
    recent = records[-last:] if last and last > 0 else list(records)

    def _tot(k: str) -> int:
        return sum(int(r.get(k, 0) or 0) for r in recent)

    created, selected = _tot("issues_created"), _tot("selected")
    rejected, merged, skipped = _tot("rejected"), _tot("merged"), _tot("skipped")
    triaged = selected + rejected
    reject_rate = (rejected / triaged) if triaged else 0.0

    # Trailing run of cycles with zero merges (integration-stuck signal).
    merged_zero_streak = 0
    for r in reversed(recent):
        if int(r.get("merged", 0) or 0) == 0:
            merged_zero_streak += 1
        else:
            break

    flags: list[str] = []
    if reject_rate > 0.70:
        flags.append(
            "HIGH_REJECT_RATE: be more selective — only high-evidence proposals"
        )
    if merged_zero_streak >= 3:
        flags.append(
            f"MERGED_ZERO x{merged_zero_streak}: integration looks stuck — check CI / flaky gates"
        )

    return {
        "cycles": len(recent),
        "issues_created": created,
        "selected": selected,
        "rejected": rejected,
        "merged": merged,
        "skipped": skipped,
        "reject_rate": round(reject_rate, 3),
        "merged_zero_streak": merged_zero_streak,
        "flags": flags,
    }


def format_summary(summary: Dict[str, Any]) -> str:
    """One-line, log/agent-friendly rendering of summarize()."""
    tail = " | ".join(summary["flags"]) if summary["flags"] else "signal OK"
    return (
        f"[evolution-funnel] last {summary['cycles']} cycles: "
        f"created={summary['issues_created']} selected={summary['selected']} "
        f"rejected={summary['rejected']} merged={summary['merged']} "
        f"skipped={summary['skipped']} reject_rate={summary['reject_rate']:.0%} "
        f"merged_zero_streak={summary['merged_zero_streak']} | {tail}"
    )


def cycle_date(now) -> str:
    """The date of the cycle to measure. This job runs in the MORNING (07:40,
    before the watchdog), so before ~08:00 the cycle that just completed is
    YESTERDAY's (research 09:00 .. integration 23:00). After 08:00, today's.
    Jitter-safe: 07:40 + scheduler jitter stays < 08:00."""
    from datetime import timedelta

    day = now.date() if now.hour >= 8 else (now - timedelta(days=1)).date()
    return day.isoformat()


def main(argv: list[str]) -> int:
    evolution_dir = Path(
        os.environ.get(
            "EVOLUTION_PROFILE_DIR",
            str(Path.home() / ".hermes" / "profiles" / "user1" / "evolution"),
        )
    )
    args = argv[1:]

    # Read-side feedback path (#84): summarize recent cycles so evolution-research
    # can self-tune selectivity. `--summary [--last N]` (default N=7).
    if "--summary" in args:
        last = 7
        if "--last" in args:
            i = args.index("--last")
            if i + 1 < len(args):
                try:
                    last = int(args[i + 1])
                except ValueError:
                    last = 7
        records = load_records(evolution_dir / "metrics.jsonl")
        print(format_summary(summarize(records, last)))
        return 0

    # Explicit arg wins (manual/backfill runs); else env; else the cycle date.
    date = argv[1] if len(argv) > 1 and not argv[1].startswith("-") else ""
    date = date or os.environ.get("EVOLUTION_FUNNEL_DATE", "")
    if not date:
        try:
            from hermes_time import now as _now  # type: ignore

            date = cycle_date(_now())
        except Exception:
            print(
                "[evolution-funnel] no date given and clock unavailable",
                file=sys.stderr,
            )
            return 1

    record = compute_funnel(evolution_dir, date)
    append_funnel(evolution_dir / "metrics.jsonl", record)

    # ── Halt detection gate (#evolution — zero-deliverables auto-halt) ──
    # When the pipeline produces zero merged PRs for 5+ consecutive cycles
    # AND zero issues selected for 3+ consecutive cycles (both signals
    # agree), emit a halt state. This prevents the pipeline from burning
    # API credits on a broken loop: cron jobs check for the halt file before
    # spawning expensive LLM stages.
    _halt_threshold_merged = 5  # cycles with merged=0
    _halt_threshold_selected = 3  # cycles with selected=0
    _halt_file = evolution_dir / "halt-state.txt"
    try:
        _all_records = load_records(evolution_dir / "metrics.jsonl")
        _summary = summarize(
            _all_records, max(_halt_threshold_merged, _halt_threshold_selected)
        )
        _merged_zero = _summary.get("merged_zero_streak", 0)
        # Count consecutive cycles with selected=0 too
        _selected_zero_streak = 0
        for r in reversed(_all_records):
            if int(r.get("selected", 0) or 0) == 0:
                _selected_zero_streak += 1
            else:
                break
        if (
            _merged_zero >= _halt_threshold_merged
            and _selected_zero_streak >= _halt_threshold_selected
        ):
            _halt_file.write_text(
                f"# Evolution pipeline HALTED\n"
                f"# Date: {date}\n"
                f"# merged_zero_streak: {_merged_zero} (threshold: {_halt_threshold_merged})\n"
                f"# selected_zero_streak: {_selected_zero_streak} (threshold: {_halt_threshold_selected})\n"
                f"# The pipeline has produced zero automated deliverables for "
                f"{_merged_zero}+ consecutive cycles. All expensive LLM stages "
                f"(research, analysis, implementation) will skip until the halt "
                f"is manually cleared.\n"
                f"#\n"
                f"# To resume: delete this file and address the root cause "
                f"(provider timeout, broken fallback, credential expiry).\n",
                encoding="utf-8",
            )
            print(
                f"[evolution-funnel] HALT DETECTED: merged=0 x{_merged_zero}, "
                f"selected=0 x{_selected_zero_streak} — wrote {_halt_file}"
            )
        elif _halt_file.exists():
            # Auto-clear the halt if metrics improved enough to drop below
            # either threshold.
            _halt_file.unlink()
            print(
                f"[evolution-funnel] HALT CLEARED: merged_zero_streak={_merged_zero}, "
                f"selected_zero_streak={_selected_zero_streak}",
            )
    except Exception as _halt_exc:
        # Never let halt detection crash the funnel job itself.
        logger.warning("Halt detection gate failed (non-fatal): %s", _halt_exc)

    # Refresh the rolling-summary sidecar so stages WITHOUT a terminal toolset
    # (evolution-research has only web+file) can consume the funnel feedback via
    # the `file` toolset — they can't run `--summary` themselves (#84 loop).
    try:
        (evolution_dir / "funnel-summary.txt").write_text(
            format_summary(summarize(load_records(evolution_dir / "metrics.jsonl"), 7))
            + "\n",
            encoding="utf-8",
        )
    except OSError:
        pass

    # Refresh the longitudinal health sidecar too (meta-evolution metrics, rec
    # 4.3): "is the pipeline improving?" readable by any file-toolset stage. Lazy
    # import avoids a module cycle (evolution_metrics imports load_records here).
    try:
        from evolution_metrics import compute_health, format_health

        (evolution_dir / "evolution-health.txt").write_text(
            format_health(
                compute_health(load_records(evolution_dir / "metrics.jsonl"), 30)
            )
            + "\n",
            encoding="utf-8",
        )
    except Exception:
        pass

    # Refresh the realized-impact sidecar (post-merge feedback loop): "did what we
    # MERGED actually help?" — read by analysis to shift into consolidation when
    # the agent is shipping plausible-but-useless code, and by the watchdog to
    # alert. Closes the blind-evolution gap (predicted impact never checked vs
    # reality). Lazy import; never let it break the funnel job.
    try:
        from evolution_realized_impact import (
            compute_realized,
            format_realized,
            load_ledger,
        )

        (evolution_dir / "realized-impact.txt").write_text(
            format_realized(
                compute_realized(
                    load_ledger(evolution_dir / "realized" / "ledger.jsonl"), today=date
                )
            )
            + "\n",
            encoding="utf-8",
        )
    except Exception:
        pass

    # Deterministic no_agent job: empty stdout = silent/healthy. Print a compact
    # one-liner only so the run log shows what was recorded.
    print(
        f"[evolution-funnel] {date}: created={record['issues_created']} "
        f"selected={record['selected']} rejected={record['rejected']} "
        f"merged={record['merged']} skipped={record['skipped']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
