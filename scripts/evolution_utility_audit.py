#!/usr/bin/env python3
"""Leave-one-out utility audit over the skill corpus (issue #2286, SkillProx).

SkillProx (arXiv:2608.07449) estimates each knowledge unit's contribution with
a frozen leave-one-out utility audit, then applies validation-gated
consolidation, demotion, or removal.  This brings that backward-stage pass to
Hermes's skill corpus as a deterministic, embedding-free audit: each skill's
utility is a recency-weighted activity score (use/view/patch from the curator
sidecar), and verdicts are ``keep`` / ``consolidate`` / ``demote`` / ``remove``.

Unlike the earlier dead-code attempt (PR #2366), this module has a real
runtime entry point (``main()``) and a real action: a ``demote`` verdict writes
a ``trust_state: demoted`` marker into the curator sidecar (``.usage.json``),
so the audit's verdicts have consequence instead of only reporting.  It is
registered as a ``no_agent`` cron job (``cron/evolution/utility-audit.yaml``)
so it runs on a schedule.

Deterministic, no LLM, never deletes skills.  The only mutation is the
``trust_state`` marker on a ``demote`` verdict, which is advisory (the curator
still owns archival).
"""

from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

HALF_LIFE_DAYS = 30.0
KEEP_FRACTION = 0.10
REDUNDANT_OVERLAP = 0.35

# Actual-use precision gate (issue #2954, slice 2 of #2897).  Research
# (arXiv:2608.14036): used-when-retrieved precision falls 29.6% -> 3.3% as
# pools grow 5 -> 100; below-threshold precision at/above min pool = signal.
PRECISION_MIN_POOL = 5
PRECISION_COLLAPSE_THRESHOLD = 0.15

# Non-stationary audit bar (issue #63): the audit standard rises with the
# system instead of freezing.  The bar carries the previous audit's accepted
# observations forward as calibration traps, tracks missed drift, and rotates
# the audit rubric when the miss threshold is crossed.  Wired in below; the
# engine lives in evolution/lib/audit_bar.py.
DEFAULT_MISS_THRESHOLD = 2


def _import_audit_bar():
    """Import the non-stationary bar engine, resolving the repo root when the
    script runs from scripts/ (the cron runner) without the repo on sys.path.

    Returns the module, or None in a standalone deployment (HERMES_HOME/scripts
    without the repo tree) — the bar then no-ops instead of crashing the
    audit, mirroring resolve_active_rubric's fallback in
    evolution_rubric_judge.py.
    """
    try:
        from evolution.lib import audit_bar

        return audit_bar
    except ImportError:
        pass
    repo_root = Path(__file__).resolve().parents[1]
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))
    try:
        from evolution.lib import audit_bar

        return audit_bar
    except ImportError:
        return None


audit_bar = _import_audit_bar()
BAR_AVAILABLE = audit_bar is not None

_STOPWORDS = frozenset(
    "the and for are but not you all any can had her was one our out has have "
    "from this that with will your tool skill agent hermes using used use when "
    "what how which into they".split()
)


def _parse_iso(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _tokens(text: Optional[str]) -> set:
    if not text:
        return set()
    out = set()
    for w in text.lower().replace("-", " ").replace("_", " ").split():
        c = "".join(ch for ch in w if ch.isalnum())
        if len(c) >= 3 and c not in _STOPWORDS:
            out.add(c)
    return out


def _jaccard(a: set, b: set) -> float:
    return 0.0 if not a or not b else len(a & b) / len(a | b)


@dataclass
class SkillAudit:
    """One skill's leave-one-out audit result."""

    name: str
    utility: float
    share: float
    activity: int
    max_overlap: float
    verdict: str


def skill_utility(record: Dict[str, Any], now: Optional[datetime] = None) -> float:
    """Recency-weighted activity score (exponential half-life decay)."""
    now = now or datetime.now(timezone.utc)
    total = 0
    for key in ("use_count", "view_count", "patch_count"):
        try:
            total += int(record.get(key) or 0)
        except (TypeError, ValueError):
            continue
    if total <= 0:
        return 0.0
    last = _parse_iso(record.get("last_used_at"))
    if last is None:
        return float(total)
    days = max(0.0, (now - last).total_seconds() / 86400.0)
    return float(total) * (0.5 ** (days / HALF_LIFE_DAYS))


def audit_corpus(
    usage: Dict[str, Dict[str, Any]],
    now: Optional[datetime] = None,
) -> List[SkillAudit]:
    """Leave-one-out utility audit over a usage map (name -> record, as
    returned by ``tools.skill_usage.load_usage()``).  Returns one
    :class:`SkillAudit` per skill, sorted by utility descending."""
    now = now or datetime.now(timezone.utc)
    utilities = {n: skill_utility(r, now) for n, r in usage.items()}
    total = sum(utilities.values())
    tokens = {
        n: _tokens(
            f"{n} {(usage[n].get('description') or usage[n].get('summary') or '')}"
        )
        for n in usage
    }

    audits: List[SkillAudit] = []
    for name, utility in utilities.items():
        share = (utility / total) if total > 0 else 0.0
        activity = 0
        for key in ("use_count", "view_count", "patch_count"):
            try:
                activity += int(usage[name].get(key) or 0)
            except (TypeError, ValueError):
                continue
        max_overlap = max(
            (_jaccard(tokens[name], t) for other, t in tokens.items() if other != name),
            default=0.0,
        )
        if share >= KEEP_FRACTION:
            verdict = "keep"
        elif activity == 0 and max_overlap == 0.0:
            verdict = "remove"
        elif max_overlap >= REDUNDANT_OVERLAP:
            verdict = "consolidate"
        else:
            verdict = "demote"
        audits.append(SkillAudit(name, utility, share, activity, max_overlap, verdict))
    return sorted(audits, key=lambda a: a.utility, reverse=True)


def _usage_file() -> Path:
    """The curator sidecar path (mirrors tools.skill_usage._usage_file)."""
    hh = os.environ.get("HERMES_HOME", "").strip()
    base = Path(hh) if hh else Path.home() / ".hermes"
    return base / "skills" / ".usage.json"


def _load_usage() -> Dict[str, Dict[str, Any]]:
    path = _usage_file()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return {k: v for k, v in data.items() if isinstance(v, dict)}


def _save_usage(data: Dict[str, Dict[str, Any]]) -> None:
    path = _usage_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False), encoding="utf-8"
    )
    os.replace(tmp, path)


def apply_demotions(
    audits: List[SkillAudit], usage: Dict[str, Dict[str, Any]]
) -> List[str]:
    """Stamp ``trust_state: demoted`` on every ``demote`` verdict.

    This is the audit's one real action (issue #2286): a low-utility,
    non-redundant skill is marked demoted in the curator sidecar so downstream
    tooling (curator, skill surfacing) can treat it as lower-trust.  Returns the
    list of skill names that were newly demoted.  Best-effort — a missing or
    corrupt sidecar is a no-op, never a crash.
    """
    demoted: List[str] = []
    for a in audits:
        if a.verdict != "demote":
            continue
        rec = usage.get(a.name)
        if not isinstance(rec, dict):
            continue
        if rec.get("trust_state") == "demoted":
            continue
        rec["trust_state"] = "demoted"
        rec["demoted_at"] = datetime.now(timezone.utc).isoformat()
        demoted.append(a.name)
    if demoted:
        _save_usage(usage)
    return demoted


@dataclass
class RetrievalPrecision:
    """Actual-use precision over skill-retrieval events (#2954).

    Precision is the used-when-retrieved rate; ``gate_triggered`` flags
    pool-growth collapse (pool >= PRECISION_MIN_POOL while precision <
    PRECISION_COLLAPSE_THRESHOLD) — the misevolution signal from
    arXiv:2608.14036.
    """

    events_analyzed: int
    retrieved_total: int
    used_total: int
    precision: float
    pool_size: int
    gate_triggered: bool


def _load_retrieval_events() -> List[Dict[str, Any]]:
    """Load retrieval events (JSONL: ts/query/retrieved) from the sidecar.

    Best-effort: missing/corrupt lines are skipped — the precision report
    must never crash the audit.
    """
    hh = os.environ.get("HERMES_HOME", "").strip()
    base = Path(hh) if hh else Path.home() / ".hermes"
    path = base / "skills" / "retrieval_events.jsonl"
    if not path.exists():
        return []
    events: List[Dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(event, dict) and isinstance(event.get("retrieved"), list):
                    events.append(event)
    except OSError:
        return []
    return events


def _used_names(usage: Dict[str, Dict[str, Any]]) -> set:
    """Skill names with recorded actual use (use_count/patch_count > 0)."""
    used = set()
    for name, rec in usage.items():
        if not isinstance(rec, dict):
            continue
        for key in ("use_count", "patch_count"):
            try:
                if int(rec.get(key) or 0) > 0:
                    used.add(name)
                    break
            except (TypeError, ValueError):
                continue
    return used


def _normalize_identifier(value: str) -> str:
    """Slugify an identifier/name so both sides of the join compare.

    Events store source identifiers (``openai/skills/foo``) while the curator
    sidecar is keyed by name (``foo``); both collapse to the same slug.
    """
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")


def retrieval_precision(
    events: List[Dict[str, Any]], usage: Dict[str, Dict[str, Any]]
) -> RetrievalPrecision:
    """Actual-use precision over retrieval events joined to usage (used =
    identifier slug/tail matches a usage name with recorded actual use)."""
    used_norms = {_normalize_identifier(n) for n in _used_names(usage)}
    retrieved_total = 0
    used_total = 0
    identifiers = set()
    for event in events:
        retrieved = [r for r in event.get("retrieved", []) if isinstance(r, str) and r]
        retrieved_total += len(retrieved)
        for ident in retrieved:
            identifiers.add(ident)
            slug = _normalize_identifier(ident)
            tail = _normalize_identifier(ident.rsplit("/", 1)[-1])
            if slug in used_norms or tail in used_norms:
                used_total += 1
    precision = (used_total / retrieved_total) if retrieved_total else 0.0
    pool_size = len(identifiers)
    gate = pool_size >= PRECISION_MIN_POOL and precision < PRECISION_COLLAPSE_THRESHOLD
    return RetrievalPrecision(
        len(events), retrieved_total, used_total, precision, pool_size, gate
    )


def _usage() -> str:
    return (
        "usage: evolution_utility_audit.py [--apply] [--json] [--bar-prompt] "
        "[--miss-threshold N]\n"
        "  Runs the leave-one-out utility audit over the curator sidecar.\n"
        "  --apply            stamp trust_state=demoted on demote verdicts (default: report only)\n"
        "  --json             emit machine-readable JSON to stdout\n"
        "  --bar-prompt       print the non-stationary audit prompt (active rubric\n"
        "                     variant + calibration traps) and exit\n"
        "  --miss-threshold N rotation threshold for the non-stationary audit bar\n"
        "                     (default 2; persisted in audit-bar-state.json)\n"
        "  Exit 0 ok, 2 bad input.\n"
    )


def _evolution_dir() -> Path:
    """Canonical evolution state dir: $EVOLUTION_PROFILE_DIR or
    <hermes-home>/evolution (mirrors evolution_watchdog.py)."""
    hermes_home = Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes")))
    return Path(os.environ.get("EVOLUTION_PROFILE_DIR", str(hermes_home / "evolution")))


def _bar_observations(audits: List[SkillAudit]) -> List[Dict[str, Any]]:
    """The audit's observations as the bar's trap pool entries."""
    return [
        {
            "kind": "skill",
            "name": a.name,
            "verdict": a.verdict,
            "utility": round(a.utility, 3),
            "share": round(a.share, 4),
            "activity": a.activity,
        }
        for a in audits
    ]


def _run_audit_bar(
    audits: List[SkillAudit],
    evolution_dir: Path,
    *,
    miss_threshold: int = DEFAULT_MISS_THRESHOLD,
    silent: bool = False,
) -> Tuple[Optional[Any], List[str], List[str]]:
    """Wire the non-stationary audit bar (issue #63) into the daily run.

    Loads the persisted bar state, detects NEW drift against the previous
    audit's accepted observations (calibration traps), reports drift findings,
    applies the miss-count / rubric-rotation state machine, and persists the
    updated state.  Returns ``(new_state, events, report_lines)``;
    ``(None, [], [])`` when the bar engine is unavailable (standalone
    deployment).  ``report_lines`` are the ``[utility-audit][bar]`` lines;
    they are printed unless ``silent`` (JSON / --bar-prompt mode keeps
    stdout clean for the caller's own document).
    """
    if not BAR_AVAILABLE:
        return None, [], []
    assert audit_bar is not None  # BAR_AVAILABLE guard above
    observations = _bar_observations(audits)
    path = audit_bar.state_file_path(evolution_dir)
    state = audit_bar.load_state(path)

    # First run — no accepted baseline yet: everything observed becomes a
    # calibration trap, nothing has drifted FROM anything yet.
    if not state.accepted_observations:
        new_state, events = audit_bar.update_bar_state(
            state,
            drift_occurred=False,
            drift_reported=False,
            observations=observations,
            miss_threshold=miss_threshold,
        )
        audit_bar.save_state(path, new_state)
        report = [
            f"[utility-audit][bar] first run — no accepted baseline; "
            f"{len(observations)} observation(s) accepted as calibration traps "
            f"(rubric variant {new_state.rubric_variant + 1}/"
            f"{len(audit_bar.AUDIT_RUBRIC_VARIANTS)})"
        ]
        if not silent:
            for line in report:
                print(line)
        return new_state, events, report

    drift = audit_bar.find_new_drift(observations, state.accepted_observations)
    missing = audit_bar.find_missing(observations, state.accepted_observations)
    drift_occurred = bool(drift) or bool(missing)
    # Deterministic audit: every detected drift IS reported in this output
    # (the miss machinery guards the LLM-auditor case and is exercised by the
    # engine's tests; a silent miss here would mean a crashed report path).
    drift_reported = drift_occurred

    new_state, events = audit_bar.update_bar_state(
        state,
        drift_occurred=drift_occurred,
        drift_reported=drift_reported,
        observations=observations,
        miss_threshold=miss_threshold,
    )
    audit_bar.save_state(path, new_state)

    report = [
        f"[utility-audit][bar] rubric variant "
        f"{new_state.rubric_variant + 1}/{len(audit_bar.AUDIT_RUBRIC_VARIANTS)}; "
        f"miss count {new_state.miss_count}/{new_state.miss_threshold}",
        f"[utility-audit][bar] calibration traps carried forward: "
        f"{len(new_state.accepted_observations)} known/accepted — flagged as "
        "known, never re-reported as new drift",
    ]
    for obs in drift:
        prev = next(
            (
                p
                for p in state.accepted_observations
                if audit_bar.observation_id(p) == audit_bar.observation_id(obs)
            ),
            None,
        )
        prev_verdict = prev.get("verdict") if prev else "?"
        report.append(
            f"[utility-audit][bar] NEW DRIFT: {obs.get('name')} "
            f"(verdict {prev_verdict} -> {obs.get('verdict')})"
        )
    for obs in missing:
        report.append(
            f"[utility-audit][bar] NEW DRIFT: {obs.get('name')} disappeared "
            f"from the corpus (was {obs.get('verdict')})"
        )
    report.extend(f"[utility-audit][bar] {event}" for event in events)
    if not silent:
        for line in report:
            print(line)
    return new_state, events, report


def main(argv: Optional[List[str]] = None) -> int:
    args = list(argv if argv is not None else sys.argv[1:])
    if "--help" in args or "-h" in args:
        print(_usage())
        return 0
    apply = "--apply" in args
    as_json = "--json" in args

    miss_threshold = DEFAULT_MISS_THRESHOLD
    if "--miss-threshold" in args:
        try:
            miss_threshold = max(1, int(args[args.index("--miss-threshold") + 1]))
        except (ValueError, IndexError):
            print(
                "evolution_utility_audit: --miss-threshold needs an integer >= 1",
                file=sys.stderr,
            )
            return 2

    usage = _load_usage()
    audits = audit_corpus(usage)
    demoted = apply_demotions(audits, usage) if apply else []
    evolution_dir = _evolution_dir()

    precision = retrieval_precision(_load_retrieval_events(), usage)

    bar_state, _, _ = _run_audit_bar(
        audits,
        evolution_dir,
        miss_threshold=miss_threshold,
        silent=(as_json or "--bar-prompt" in args),
    )

    if "--bar-prompt" in args:
        if bar_state is not None:
            print(
                audit_bar.build_audit_prompt(
                    audit_bar.AUDIT_RUBRIC_VARIANTS,
                    bar_state.rubric_variant,
                    bar_state.accepted_observations,
                    bar_state.miss_threshold,
                )
            )
        else:
            print(
                "evolution_utility_audit: non-stationary bar unavailable "
                "(evolution lib not importable)",
                file=sys.stderr,
            )
            return 2
        return 0

    if as_json:
        bar_payload = None
        if bar_state is not None:
            bar_payload = {
                "rubric_variant": bar_state.rubric_variant,
                "rubric_variants": len(audit_bar.AUDIT_RUBRIC_VARIANTS),
                "miss_count": bar_state.miss_count,
                "miss_threshold": bar_state.miss_threshold,
                "calibration_traps": len(bar_state.accepted_observations),
                "rotations": len(bar_state.rotations),
            }
        print(
            json.dumps(
                {
                    "audited": len(audits),
                    "verdicts": {
                        v: sum(1 for a in audits if a.verdict == v)
                        for v in ("keep", "consolidate", "demote", "remove")
                    },
                    "demoted": demoted,
                    "audit_bar": bar_payload,
                    "retrieval_precision": {
                        "events": precision.events_analyzed,
                        "retrieved": precision.retrieved_total,
                        "used": precision.used_total,
                        "precision": round(precision.precision, 4),
                        "pool_size": precision.pool_size,
                        "gate_triggered": precision.gate_triggered,
                    },
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 0

    for a in audits:
        print(
            f"[utility-audit] {a.name}: {a.verdict} (utility={a.utility:.2f}, share={a.share:.3f}, activity={a.activity})"
        )
    if demoted:
        print(f"[utility-audit] demoted {len(demoted)} skill(s): {', '.join(demoted)}")
    print(
        f"[utility-audit][precision] events={precision.events_analyzed} "
        f"retrieved={precision.retrieved_total} used={precision.used_total} "
        f"precision={precision.precision:.1%} pool={precision.pool_size}"
        + (
            " [GATE: pool-growth precision collapse]"
            if precision.gate_triggered
            else ""
        )
    )
    print(f"[utility-audit] audited {len(audits)} skill(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
