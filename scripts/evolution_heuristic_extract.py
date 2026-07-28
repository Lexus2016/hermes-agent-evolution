#!/usr/bin/env python3
"""ERL Slice A (#1359) — Extract reusable heuristics from recorded trajectories.

Reads trajectory JSON files from ``~/.hermes/evolution/trajectories/`` and
produces a ranked heuristic list written to
``~/.hermes/evolution/heuristics/{date}.json``.

Each heuristic captures a recurring (tool, result_status, outcome) pattern
that correlates with success or failure across multiple tasks. The heuristic
is scored by cross-task frequency and outcome correlation (no LLM — pure
keyword/rule extraction so it is deterministic and fast).

Output schema (per heuristic):
    {
        "pattern": "terminal:success",           # tool:status signature
        "source_trajectories": ["2026-07-26_..."],
        "frequency": 5,                           # how many tasks hit this
        "success_rate": 0.80,                     # fraction of success outcomes
        "outcome_score": 0.80,                    # success_rate weighted by freq
        "recommendation": "Prefer terminal for shell operations"
    }

CLI:
    python scripts/evolution_heuristic_extract.py [--trajectories-dir DIR]
                                                  [--output-dir DIR]
                                                  [--min-frequency 2]
"""

from __future__ import annotations

import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# ── Paths ────────────────────────────────────────────────────────────

_DEFAULT_TRAJECTORIES_DIR = os.path.expanduser(
    "~/.hermes/evolution/trajectories"
)
_DEFAULT_OUTPUT_DIR = os.path.expanduser("~/.hermes/evolution/heuristics")

# Recommendation templates keyed by (tool, status_prefix).
# status_prefix is the first word of result_status (e.g. "success", "error",
# "timeout").  Only a small curated set — unknown patterns fall through to
# a generic message.
_RECOMMENDATIONS: Dict[Tuple[str, str], str] = {
    ("terminal", "success"): "Terminal commands are effective for this task type",
    ("terminal", "error"): "Avoid repeated terminal commands that fail; use read_file/write_file for filesystem ops",
    ("terminal", "timeout"): "Terminal commands time out for this task; consider execute_code or splitting the command",
    ("read_file", "success"): "read_file is effective for content inspection",
    ("read_file", "error"): "Verify file paths exist before reading; use search_files to locate files",
    ("patch", "success"): "patch is effective for targeted edits",
    ("patch", "error"): "Verify the target file exists and old_string is unique before patching",
    ("search_files", "success"): "search_files is effective for locating content",
    ("write_file", "success"): "write_file is effective for creating files",
}


def _load_trajectories(trajectories_dir: str) -> List[Dict[str, Any]]:
    """Load all trajectory JSON files, returning a flat list of entries."""
    tdir = Path(trajectories_dir)
    if not tdir.is_dir():
        return []
    entries: List[Dict[str, Any]] = []
    for fpath in sorted(tdir.glob("*.json")):
        try:
            with open(fpath, encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError):
            continue
        # Each file is either a dict with 'entries' or a bare list.
        if isinstance(data, dict):
            file_entries = data.get("entries", [])
        elif isinstance(data, list):
            file_entries = data
        else:
            continue
        for entry in file_entries:
            if isinstance(entry, dict):
                entry["_source_file"] = fpath.name
                entries.append(entry)
    return entries


def _extract_patterns(
    entries: List[Dict[str, Any]],
) -> Dict[str, Dict[str, Any]]:
    """Group entries by (tool, result_status) and compute per-pattern stats.

    Returns a dict keyed by pattern signature, each value containing:
        tools_seen, statuses, source_files, success_count, total_count.
    """
    groups: Dict[str, Dict[str, Any]] = defaultdict(
        lambda: {
            "source_files": set(),
            "success_count": 0,
            "total_count": 0,
        }
    )
    for entry in entries:
        tool = str(entry.get("tool", "unknown"))
        status = str(entry.get("result_status", "unknown"))
        sig = f"{tool}:{status}"
        g = groups[sig]
        g["total_count"] += 1
        if status == "success":
            g["success_count"] += 1
        src = entry.get("_source_file", "unknown")
        if src:
            g["source_files"].add(src)
    return groups


def _build_heuristics(
    groups: Dict[str, Dict[str, Any]],
    min_frequency: int = 2,
) -> List[Dict[str, Any]]:
    """Convert grouped patterns into ranked heuristic dicts."""
    heuristics: List[Dict[str, Any]] = []
    for sig, g in groups.items():
        freq = g["total_count"]
        if freq < min_frequency:
            continue
        success_rate = g["success_count"] / freq if freq > 0 else 0.0
        # outcome_score = success_rate weighted by frequency (log-like dampening
        # so a pattern seen 2x at 100% doesn't outrank one seen 50x at 90%).
        outcome_score = round(success_rate * (1 - 1 / (freq + 1)), 4)
        tool, status = sig.split(":", 1) if ":" in sig else (sig, "")
        status_prefix = status.split("_")[0] if status else ""
        rec = _RECOMMENDATIONS.get(
            (tool, status_prefix),
            f"Pattern '{sig}' observed {freq}x — review for task-fit",
        )
        heuristics.append(
            {
                "pattern": sig,
                "tool": tool,
                "status": status,
                "source_trajectories": sorted(g["source_files"]),
                "frequency": freq,
                "success_rate": round(success_rate, 4),
                "outcome_score": outcome_score,
                "recommendation": rec,
            }
        )
    # Sort by outcome_score desc, then frequency desc.
    heuristics.sort(
        key=lambda h: (-h["outcome_score"], -h["frequency"])
    )
    return heuristics


def extract_heuristics(
    trajectories_dir: str = _DEFAULT_TRAJECTORIES_DIR,
    min_frequency: int = 2,
) -> List[Dict[str, Any]]:
    """Run the full extraction pipeline and return ranked heuristics."""
    entries = _load_trajectories(trajectories_dir)
    if not entries:
        return []
    groups = _extract_patterns(entries)
    return _build_heuristics(groups, min_frequency)


def run(
    trajectories_dir: str = _DEFAULT_TRAJECTORIES_DIR,
    output_dir: str = _DEFAULT_OUTPUT_DIR,
    min_frequency: int = 2,
) -> str:
    """Extract heuristics and write to ``output_dir/{date}.json``.

    Returns the output file path.
    """
    heuristics = extract_heuristics(trajectories_dir, min_frequency)
    os.makedirs(output_dir, exist_ok=True)
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    out_path = os.path.join(output_dir, f"{date_str}.json")
    payload = {
        "date": date_str,
        "trajectories_dir": trajectories_dir,
        "heuristic_count": len(heuristics),
        "heuristics": heuristics,
    }
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
    return out_path


def main(argv: Optional[List[str]] = None) -> int:
    argv = argv or sys.argv[1:]
    import argparse

    parser = argparse.ArgumentParser(
        description="Extract cross-task heuristics from trajectory data"
    )
    parser.add_argument(
        "--trajectories-dir",
        default=_DEFAULT_TRAJECTORIES_DIR,
        help="Directory containing trajectory JSON files",
    )
    parser.add_argument(
        "--output-dir",
        default=_DEFAULT_OUTPUT_DIR,
        help="Directory for output heuristic files",
    )
    parser.add_argument(
        "--min-frequency",
        type=int,
        default=2,
        help="Minimum cross-task frequency to include a heuristic",
    )
    args = parser.parse_args(argv)
    out = run(
        trajectories_dir=args.trajectories_dir,
        output_dir=args.output_dir,
        min_frequency=args.min_frequency,
    )
    heuristics = extract_heuristics(
        args.trajectories_dir, args.min_frequency
    )
    print(f"Extracted {len(heuristics)} heuristics → {out}")
    for h in heuristics[:10]:
        print(
            f"  [{h['outcome_score']:.2f}] {h['pattern']} "
            f"(freq={h['frequency']}, sr={h['success_rate']:.2f})"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
