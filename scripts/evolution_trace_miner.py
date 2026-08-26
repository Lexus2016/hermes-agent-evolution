#!/usr/bin/env python3
"""Self-Harness trace miner — first increment (#248).

The evolution pipeline is fed almost entirely by the nightly research job: a
single external signal of "what should we improve?". The much richer signal is
already on disk — the agent's OWN execution traces — and #238 taught
``introspection_extract`` to read them (``*.jsonl`` + ``request_dump_*.json``)
and emit an aggregated, anonymized digest of recurring failures.

This miner is the next layer: it turns that digest into structured **weakness
records** that the ``evolution-issues`` stage can consume alongside the research
report. A weakness record is just a typed cluster that recurred at least
``min_count`` times — no new pipeline, no LLM, no raw content. It is the
deterministic core of the Self-Harness loop (arXiv 2606.09498); the LLM
harness-proposal generator and the regression gate are deliberately deferred to
later increments.

Output: JSON weakness records to stdout, and (in main) a sidecar
``<evolution_dir>/weaknesses-<cycle>.json`` the issues stage reads.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict, List

sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    from introspection_extract import _sessions_dir, build_digest
except Exception:  # pragma: no cover - keep import path explicit on failure
    raise

DEFAULT_MIN_COUNT = 5  # a cluster must recur this often to become a weakness

# Per-kind diagnosis skeleton for the AutoSaddler-style harness optimization slice
# (#3209 increment 1).  Each weakness is mapped to a component scope and a likely
# root cause so the downstream proposer can generate typed, scoped edits rather
# than broad prose rewrites.  This layer is deterministic and uses only the
# anonymized fields already emitted by the miner.
DIAGNOSIS_TEMPLATES: Dict[str, Dict[str, str]] = {
    "tool_failure": {
        "component_scope": "tool_wrapper",
        "likely_root_cause": "argument validation or result-shape mismatch",
        "failure_mode": "tool results look like failures across multiple sessions",
        "recommended_harness_surface": "tool_guard",
    },
    "provider_error": {
        "component_scope": "retry_policy",
        "likely_root_cause": "retry/fallback policy not tuned for this error class",
        "failure_mode": "provider-layer error recurs before recovery succeeds",
        "recommended_harness_surface": "retry_policy_change",
    },
    "retry_spiral": {
        "component_scope": "retry_policy",
        "likely_root_cause": "uncapped consecutive retries on a failing tool",
        "failure_mode": "same tool invoked many times in a row without recovery",
        "recommended_harness_surface": "retry_policy_change",
    },
}


def diagnose_record(record: Dict[str, Any]) -> Dict[str, Any]:
    """Attach a deterministic, component-scoped diagnosis skeleton to a weakness
    record.  The diagnosis never uses raw trace content; it only reflects the
    anonymized kind + counts already present.  Returns a fresh dict so the input
    record is not mutated in place."""
    out = dict(record)
    tmpl = DIAGNOSIS_TEMPLATES.get(out.get("kind"), {})
    if tmpl:
        out["diagnosis"] = {
            "component_scope": tmpl["component_scope"],
            "likely_root_cause": tmpl["likely_root_cause"],
            "failure_mode": tmpl["failure_mode"],
            "recommended_harness_surface": tmpl["recommended_harness_surface"],
            "severity": out.get("severity", 0),
        }
    return out


def mine_weaknesses(digest: Dict[str, Any], min_count: int = DEFAULT_MIN_COUNT) -> List[Dict[str, Any]]:
    """Turn an introspection digest's aggregated signals into structured weakness
    records for clusters that recur >= ``min_count`` times. Pure + deterministic;
    operates only on the anonymized digest, never raw traces."""
    signals = digest.get("signals", {}) if isinstance(digest, dict) else {}
    window = digest.get("window_days", 0) if isinstance(digest, dict) else 0
    records: List[Dict[str, Any]] = []

    # Repeated tool failures attributed to a tool.
    for tool, n in (signals.get("tool_failures") or {}).items():
        if isinstance(n, int) and n >= min_count:
            records.append({
                "kind": "tool_failure",
                "tool": tool,
                "occurrences": n,
                "severity": n,
                "label": f"`{tool}` results look like failures {n}x in {window}d — "
                         f"harden the tool wrapper or its preconditions.",
            })

    # Provider-layer errors, keyed by recovery class (#236 failure_category).
    for sig, n in (signals.get("provider_errors") or {}).items():
        if isinstance(n, int) and n >= min_count:
            records.append({
                "kind": "provider_error",
                "signature": sig,
                "occurrences": n,
                "severity": n,
                "label": f"provider error `{sig}` recurs {n}x — check fallback chain / "
                         f"model-provider config for this class.",
            })

    # Retry spirals (the loop_guard shape): same tool many times in a row.
    for tool, info in (signals.get("repeated_tool_runs") or {}).items():
        if not isinstance(info, dict):
            continue
        runs = info.get("max_consecutive", 0)
        if isinstance(runs, int) and runs >= min_count:
            records.append({
                "kind": "retry_spiral",
                "tool": tool,
                "max_consecutive": runs,
                "sessions": info.get("sessions", 0),
                "severity": runs,
                "label": f"`{tool}` retry spiral up to {runs} consecutive across "
                         f"{info.get('sessions', 0)} session(s) — add a non-retryable "
                         f"diagnostic or a fallback path.",
            })

    records.sort(key=lambda r: -int(r.get("severity") or 0))
    return [diagnose_record(r) for r in records]


def format_weaknesses(records: List[Dict[str, Any]], window_days: int = 0) -> str:
    """One-line-per-record human summary for the issues-stage prompt / watchdog."""
    if not records:
        return f"[evolution-trace-miner] no recurring weaknesses (window {window_days}d)"
    lines = [f"[evolution-trace-miner] {len(records)} weakness cluster(s), window {window_days}d:"]
    for r in records:
        lines.append(f"  - [{r['kind']}] {r['label']}")
    return "\n".join(lines)


def main(argv: List[str]) -> int:
    days = 7
    min_count = DEFAULT_MIN_COUNT
    for a in argv[1:]:
        if a.startswith("--days="):
            try:
                days = int(a.split("=", 1)[1])
            except ValueError:
                pass
        elif a.startswith("--min-count="):
            try:
                min_count = int(a.split("=", 1)[1])
            except ValueError:
                pass

    sessions_dir = _sessions_dir()
    digest = build_digest(sessions_dir, window_days=days)
    records = mine_weaknesses(digest, min_count=min_count)

    payload = {
        "window_days": days,
        "min_count": min_count,
        "sessions_scanned": digest.get("sessions_scanned", 0),
        "weaknesses": records,
    }
    print(json.dumps(payload, indent=2, sort_keys=True))

    # Sidecar for the issues stage: <evolution_dir>/weaknesses-latest.json.
    # evolution_dir is the sessions dir's parent's evolution profile, mirroring
    # how the funnel writes its sidecars. Best-effort; never fail the run on IO.
    try:
        import os

        prof = os.environ.get("EVOLUTION_PROFILE_DIR")
        if prof:
            out = Path(prof) / "weaknesses-latest.json"
            out.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    except Exception:  # pragma: no cover - environment dependent
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
