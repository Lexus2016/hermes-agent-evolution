#!/usr/bin/env python3
"""Canonical version line per promoted skill (issue #1448, Child D of #1308).

Closes out the AgentDevel promotion gate. Child A (#1355) defined the probe
sets, Child B (#1446) the flip gate that scores a candidate against them, Child
C (#1447) wired the verdict into merge verification so a regression blocks. What
was still missing is the record of *what was promoted and what approved it* —
without which a regression is reverted by reconstructing history rather than by
version.

AgentDevel's argument (arXiv:2601.04620) is that self-evolution is release
engineering, and a release you cannot roll back is not a release. A version line
makes "which version is live, and what approved it?" answerable in one lookup.

Each promotion appends one line to ``<store>/<skill>.jsonl``:

    {"skill": "evolution-analysis", "version": 3,
     "promoted_at": "2026-07-28T12:00:00+00:00",
     "flip_verdict": "promote", "regressions": [], "fixes": ["p3"],
     "diff_ref": "abc1234", "critic_ref": "...", "note": "..."}

Append-only on purpose: the history IS the artifact. Overwriting would leave the
current version with no record of what it replaced, which is the position this
issue exists to get out of.

Deterministic — no LLM, no network. Pure functions plus a thin CLI, matching the
``evolution_*.py`` idiom.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

SCHEMA_VERSION = "1"


def _default_store() -> Path:
    env = os.environ.get("EVOLUTION_PROFILE_DIR", "").strip()
    if env:
        return Path(env) / "skill_versions"
    hh = os.environ.get("HERMES_HOME", "").strip()
    return (
        Path(hh) / "evolution" / "skill_versions"
        if hh
        else Path.home() / ".hermes" / "evolution" / "skill_versions"
    )


@dataclass
class SkillVersion:
    """One promotion of one skill, with the evidence that approved it."""

    skill: str
    version: int
    promoted_at: str = ""
    flip_verdict: str = ""
    regressions: List[str] = field(default_factory=list)
    fixes: List[str] = field(default_factory=list)
    diff_ref: str = ""
    critic_ref: str = ""
    note: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "skill": self.skill,
            "version": self.version,
            "promoted_at": self.promoted_at,
            "flip_verdict": self.flip_verdict,
            "regressions": list(self.regressions),
            "fixes": list(self.fixes),
            "diff_ref": self.diff_ref,
            "critic_ref": self.critic_ref,
            "note": self.note,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "SkillVersion":
        return cls(
            skill=str(data.get("skill", "")),
            version=int(data.get("version", 0) or 0),
            promoted_at=str(data.get("promoted_at", "")),
            flip_verdict=str(data.get("flip_verdict", "")),
            regressions=list(data.get("regressions", []) or []),
            fixes=list(data.get("fixes", []) or []),
            diff_ref=str(data.get("diff_ref", "")),
            critic_ref=str(data.get("critic_ref", "")),
            note=str(data.get("note", "")),
        )


def _path(skill: str, store_dir: Optional[Path] = None) -> Path:
    base = store_dir or _default_store()
    safe = skill.replace("/", "_").replace(" ", "_")
    return base / f"{safe}.jsonl"


def load_versions(skill: str, store_dir: Optional[Path] = None) -> List[SkillVersion]:
    """Every recorded promotion of ``skill``, oldest first.

    Malformed lines are skipped rather than raising: a corrupted entry must not
    make the rest of a skill's history unreadable, since the history is what a
    rollback depends on.
    """
    path = _path(skill, store_dir)
    if not path.exists():
        return []
    out: List[SkillVersion] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            data = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(data, dict) and data.get("skill"):
            out.append(SkillVersion.from_dict(data))
    return out


def current_version(skill: str, store_dir: Optional[Path] = None) -> Optional[SkillVersion]:
    """The live version — the highest recorded, not merely the last written.

    Ordering by ``version`` rather than by file position means an out-of-order
    append (a retried promotion, a merged history) cannot make an older version
    look current.
    """
    versions = load_versions(skill, store_dir)
    if not versions:
        return None
    return max(versions, key=lambda v: v.version)


def record_promotion(
    skill: str,
    *,
    flip_verdict: str = "",
    regressions: Optional[List[str]] = None,
    fixes: Optional[List[str]] = None,
    diff_ref: str = "",
    critic_ref: str = "",
    note: str = "",
    promoted_at: Optional[str] = None,
    store_dir: Optional[Path] = None,
) -> SkillVersion:
    """Append the next version for ``skill`` and return it.

    The version number is derived from what is on disk, not passed in, so two
    callers cannot disagree about which number is next.
    """
    previous = current_version(skill, store_dir)
    version = (previous.version + 1) if previous else 1
    entry = SkillVersion(
        skill=skill,
        version=version,
        promoted_at=promoted_at or datetime.now(timezone.utc).isoformat(),
        flip_verdict=flip_verdict,
        regressions=list(regressions or []),
        fixes=list(fixes or []),
        diff_ref=diff_ref,
        critic_ref=critic_ref,
        note=note,
    )
    path = _path(skill, store_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry.to_dict(), sort_keys=True) + "\n")
    return entry


def rollback_target(
    skill: str, store_dir: Optional[Path] = None
) -> Optional[SkillVersion]:
    """The version to revert to — the highest below the current one.

    Answers the question this issue exists for: a regression is reverted BY
    VERSION, with the evidence that approved that version attached, instead of
    being reconstructed from commit history.
    """
    versions = sorted(load_versions(skill, store_dir), key=lambda v: v.version)
    return versions[-2] if len(versions) >= 2 else None


def format_current(skill: str, store_dir: Optional[Path] = None) -> str:
    """One line answering 'which version is live, and what approved it?'."""
    cur = current_version(skill, store_dir)
    if cur is None:
        return f"[skill-version] {skill}: no recorded promotions"
    approved = cur.flip_verdict or "unrecorded"
    detail = ""
    if cur.fixes or cur.regressions:
        detail = f" (+{len(cur.fixes)} fixed, -{len(cur.regressions)} regressed)"
    ref = f" diff={cur.diff_ref}" if cur.diff_ref else ""
    return (
        f"[skill-version] {skill}: v{cur.version} "
        f"promoted {cur.promoted_at or 'at unknown time'} "
        f"via flip-gate '{approved}'{detail}{ref}"
    )


def _usage() -> str:
    return (
        "usage: evolution_skill_version.py <command> [args]\n"
        "  record <skill> [--verdict V] [--diff REF] [--critic REF] [--note N]\n"
        "         [--fixes a,b] [--regressions c,d]\n"
        "  current <skill>          which version is live, and what approved it\n"
        "  rollback <skill>         the version to revert to\n"
        "  history <skill>          every promotion, oldest first\n"
        "  Exit 0 ok, 2 bad input, 1 nothing to report."
    )


def _opt(args: List[str], name: str, default: str = "") -> str:
    if name in args:
        i = args.index(name)
        if i + 1 < len(args):
            return args[i + 1]
    return default


def main(argv: Optional[List[str]] = None) -> int:
    args = list(argv if argv is not None else sys.argv[1:])
    if not args or "--help" in args or "-h" in args:
        print(_usage())
        return 0 if args else 2

    cmd = args[0]
    if len(args) < 2:
        print(_usage(), file=sys.stderr)
        return 2
    skill = args[1]

    if cmd == "record":
        entry = record_promotion(
            skill,
            flip_verdict=_opt(args, "--verdict"),
            diff_ref=_opt(args, "--diff"),
            critic_ref=_opt(args, "--critic"),
            note=_opt(args, "--note"),
            fixes=[x for x in _opt(args, "--fixes").split(",") if x],
            regressions=[x for x in _opt(args, "--regressions").split(",") if x],
        )
        print(json.dumps(entry.to_dict(), indent=2, sort_keys=True))
        return 0

    if cmd == "current":
        cur = current_version(skill)
        if cur is None:
            print(format_current(skill), file=sys.stderr)
            return 1
        print(json.dumps(cur.to_dict(), indent=2, sort_keys=True))
        print(format_current(skill), file=sys.stderr)
        return 0

    if cmd == "rollback":
        target = rollback_target(skill)
        if target is None:
            print(f"[skill-version] {skill}: no earlier version to roll back to",
                  file=sys.stderr)
            return 1
        print(json.dumps(target.to_dict(), indent=2, sort_keys=True))
        return 0

    if cmd == "history":
        versions = load_versions(skill)
        if not versions:
            print(f"[skill-version] {skill}: no recorded promotions", file=sys.stderr)
            return 1
        print(json.dumps([v.to_dict() for v in versions], indent=2, sort_keys=True))
        return 0

    print(_usage(), file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
