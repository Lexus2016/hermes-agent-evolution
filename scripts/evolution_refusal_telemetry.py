#!/usr/bin/env python3
"""Refusal-nudge telemetry analyzer for soft-capability declinations (#1327).

Reads refusal-nudge telemetry logs and computes transition rates per refusal category
to determine which refusal categories resist nudging.
"""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List


def parse_refusal_telemetry_file(filepath: Path | str) -> List[Dict[str, Any]]:
    """Parse a refusal telemetry jsonl or json file."""
    path = Path(filepath)
    if not path.exists():
        return []

    records = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
                records.append(data)
            except json.JSONDecodeError:
                continue
    return records


def analyze_refusal_telemetry(records: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Analyze refusal telemetry records to compute per-category transition rates.

    Each record expects:
    - category: str (e.g., "no_tool", "access_denied", "unsupported_feature")
    - nudge_tier: str (e.g., "gentle_substitute", "script_proposal")
    - shifted: bool (True if the agent proposed an alternative after nudge)
    """
    category_counts: Dict[str, int] = defaultdict(int)
    category_shifts: Dict[str, int] = defaultdict(int)
    tier_counts: Dict[str, int] = defaultdict(int)
    tier_shifts: Dict[str, int] = defaultdict(int)

    for rec in records:
        cat = str(rec.get("category", "unknown"))
        tier = str(rec.get("nudge_tier", "default"))
        shifted = bool(rec.get("shifted", False))

        category_counts[cat] += 1
        tier_counts[tier] += 1
        if shifted:
            category_shifts[cat] += 1
            tier_shifts[tier] += 1

    category_rates = {}
    for cat, total in category_counts.items():
        shifts = category_shifts[cat]
        category_rates[cat] = {
            "total": total,
            "shifted": shifts,
            "shift_rate": round(shifts / total, 4) if total > 0 else 0.0,
        }

    return {
        "total_refusals": len(records),
        "by_category": category_rates,
        "resisting_categories": [
            cat for cat, data in category_rates.items() if data["shift_rate"] < 0.5
        ],
    }
