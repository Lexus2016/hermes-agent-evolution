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
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

HALF_LIFE_DAYS = 30.0
KEEP_FRACTION = 0.10
REDUNDANT_OVERLAP = 0.35

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


def _usage() -> str:
    return (
        "usage: evolution_utility_audit.py [--apply] [--json]\n"
        "  Runs the leave-one-out utility audit over the curator sidecar.\n"
        "  --apply   stamp trust_state=demoted on demote verdicts (default: report only)\n"
        "  --json    emit machine-readable JSON to stdout\n"
        "  Exit 0 ok, 2 bad input.\n"
    )


def main(argv: Optional[List[str]] = None) -> int:
    args = list(argv if argv is not None else sys.argv[1:])
    if "--help" in args or "-h" in args:
        print(_usage())
        return 0
    apply = "--apply" in args
    as_json = "--json" in args

    usage = _load_usage()
    audits = audit_corpus(usage)
    demoted = apply_demotions(audits, usage) if apply else []

    if as_json:
        print(
            json.dumps(
                {
                    "audited": len(audits),
                    "verdicts": {
                        v: sum(1 for a in audits if a.verdict == v)
                        for v in ("keep", "consolidate", "demote", "remove")
                    },
                    "demoted": demoted,
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
    print(f"[utility-audit] audited {len(audits)} skill(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
