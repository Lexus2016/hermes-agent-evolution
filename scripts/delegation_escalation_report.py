#!/usr/bin/env python3
"""Read delegation trajectories and optionally fit first-visit Monte Carlo values.

Rewards require a separate, operator-reviewed outcome file; a child's claim of
completion is not a training label. No provider calls or automatic exploration.
"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path


def runs(directory, date=None):
    for path in sorted(Path(directory).glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if date and data.get("date") != date:
                continue
            entries = data.get("entries", [])
            reports = [e.get("args_summary", {}) for e in entries if e.get("tool") == "delegation_escalation_report"]
            if reports and reports[-1].get("schema") == "delegation-escalation-v1":
                report = reports[-1]
                contacts, consultations = report.get("contacts"), report.get("consultations")
                cost = report.get("added_cost_usd")
                if (type(contacts) is not int or type(consultations) is not int
                        or not 0 <= consultations <= contacts
                        or cost is not None and (type(cost) not in (int, float)
                            or not math.isfinite(cost) or cost < 0)):
                    continue
                yield data, report
        except (OSError, ValueError, TypeError, AttributeError):
            continue


def summarize(directory, date=None, verified_outcomes=None):
    records = list(runs(directory, date))
    reports = [r for _, r in records]
    contacts = sum(r.get("contacts", 0) for r in reports)
    consultations = sum(r.get("consultations", 0) for r in reports)
    costs = [r.get("added_cost_usd") for r in reports]
    paired = []
    for data, report in records:
        outcome = (verified_outcomes or {}).get(data.get("session_id"), {})
        if (isinstance(outcome, dict) and report.get("consultations", 0) > 0 and outcome.get("verified") is True
                and outcome.get("paired_baseline_verified") is True
                and isinstance(outcome.get("baseline_evidence"), str)
                and outcome["baseline_evidence"].strip()
                and outcome.get("reward") in (0, 1)
                and outcome.get("baseline_reward") in (0, 1)):
            paired.append(outcome)
    return {"runs": len(reports), "contacts": contacts, "consultations": consultations,
            "consult_rate": consultations / contacts if contacts else 0,
            "added_cost_usd": sum(costs) if all(c is not None for c in costs) else None,
            "paired_runs": len(paired),
            "recovered_failures_vs_always_proceed": sum(
                p["reward"] == 1 and p["baseline_reward"] == 0 for p in paired
            ) if paired else None,
            "baseline_status": "verified paired outcomes" if paired else "unmeasured; no paired counterfactual"}


def fit(directory, verified_outcomes, price):
    """First-visit state/action means of final reward minus remaining consult cost.

    Price penalizes each future consultation once; runtime subtracts the current
    consultation price when selecting, so that immediate price is not duplicated.
    Rebuild from immutable trajectories rather than counting repeated CLI runs.
    """
    if not math.isfinite(price) or price < 0:
        raise ValueError("price must be finite and non-negative")
    totals, counts = {}, {}
    for data, _ in runs(directory):
        outcome = verified_outcomes.get(data.get("session_id"), {})
        if not isinstance(outcome, dict):
            continue
        reward = outcome.get("reward")
        if outcome.get("verified") is not True or isinstance(reward, bool) or not isinstance(reward, (float, int)) or not math.isfinite(reward) or not 0 <= reward <= 1:
            continue
        contacts = [e["args_summary"] for e in data["entries"] if e.get("tool") == "delegation_contact"]
        seen = set()
        for i, contact in enumerate(contacts):
            state, action = contact.get("state"), contact.get("action")
            if state not in {"start", "success", "failure", "repeated_failure", "denied"} or action not in {"SILENT", "CONSULT"}:
                continue
            key = state, action
            if key in seen:
                continue
            seen.add(key)
            future = sum(bool(c.get("consultation_attempted")) for c in contacts[i + 1:])
            totals[key] = totals.get(key, 0) + reward - price * future
            counts[key] = counts.get(key, 0) + 1
    values, samples = {}, {}
    for (state, action), total in totals.items():
        values.setdefault(state, {})[action] = total / counts[state, action]
        samples.setdefault(state, {})[action] = counts[state, action]
    return {"schema": "delegation-action-values-v1", "training_price": price,
            "values": values, "samples": samples}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("trajectories", type=Path)
    parser.add_argument("--verified-outcomes", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--price", type=float, default=0.5)
    args = parser.parse_args()
    if args.verified_outcomes and args.output:
        result = fit(args.trajectories, json.loads(args.verified_outcomes.read_text(encoding="utf-8")), args.price)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        temp = args.output.with_suffix(".tmp")
        temp.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
        temp.replace(args.output)
    else:
        outcomes = json.loads(args.verified_outcomes.read_text(encoding="utf-8")) if args.verified_outcomes else None
        print(json.dumps(summarize(args.trajectories, verified_outcomes=outcomes), indent=2))


if __name__ == "__main__":
    main()
