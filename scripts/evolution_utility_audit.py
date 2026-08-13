#!/usr/bin/env python3
"""Leave-one-out utility audit over the skill corpus (issue #2286, SkillProx).

SkillProx (arXiv:2608.07449) estimates each knowledge unit's contribution with
a frozen leave-one-out utility audit, then applies validation-gated
consolidation, demotion, or removal.  This brings that backward-stage pass to
Hermes's skill corpus as a deterministic, embedding-free audit: each skill's
utility is a recency-weighted activity score (use/view/patch from the curator
sidecar), and verdicts are ``keep`` / ``consolidate`` / ``demote`` / ``remove``.
Deterministic, no LLM, never mutates skills.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
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
