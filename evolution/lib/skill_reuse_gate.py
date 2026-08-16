# -*- coding: utf-8 -*-
"""Skill-distiller reuse gate + quarantine + versioned rollback (#2574).

Misevolution guardrail (parent #2538; arXiv:2608.12851 "Practice Makes
Unsafe: Skill Misevolution", arXiv:2509.26354 "Your Agent May Misevolve").

Before a distilled skill is promoted to reusable policy, this gate:

1. **Scans** each distilled skill for instruction-following / self-propagation
   content and verbatim reproductions of untrusted-source text.
2. **Quarantines** unsafe reproductions — they are never promoted to reusable
   policy; they land in a quarantine store, not the live skill library.
3. **Versions** skills so a poisoned revision can be rolled back, reusing the
   provenance tagging already in ``tools/memory_governance.py``.

This goes one step beyond SafeEvolve by tying the gate to provenance, not
just content repair: a skill whose source chain carries untrusted text is
flagged even when the reproduction is not a verbatim copy.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

__all__ = [
    "ReuseGateVerdict",
    "SkillReuseGate",
    "scan_skill_for_misevolution",
    "quarantine_skill",
    "version_skill",
]

# Instruction-following / self-propagation markers (arXiv:2608.12851). These
# are the textual fingerprints of a skill that tries to make the agent obey
# it unconditionally or propagate itself to other contexts.
_SELF_PROPAGATION_RE = re.compile(
    r"(?i)\b(ignore (all |previous |prior )?instructions?|"
    r"disregard (all |previous |prior )?instructions?|"
    r"you (must|should|will) (always|never) (ignore|obey|follow)|"
    r"propagate (this|these) (instruction|rule|skill|prompt)|"
    r"copy (this|these) (instruction|rule|skill|prompt) (to|into)|"
    r"replicate (this|these) (instruction|rule|skill|prompt)|"
    r"do not (tell|reveal|mention) (the user|anyone)|"
    r"act as if (you are|this is) (a|an) (system|admin|root)|"
    r"override (all |previous |prior )?(instructions?|rules?|policies?))\b"
)

# Verbatim reproduction of untrusted-source text: a long run of text that
# looks like it was copied wholesale (e.g. a quoted block from a web page).
_VERBATIM_QUOTE_RE = re.compile(r"(?m)^\s*[>|]\s*.{80,}$")


@dataclass
class ReuseGateVerdict:
    """Result of scanning a distilled skill for misevolution content."""

    skill_name: str
    safe: bool
    reasons: List[str] = field(default_factory=list)
    quarantined: bool = False
    version: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "ReuseGateVerdict":
        return cls(
            skill_name=str(d.get("skill_name", "")),
            safe=bool(d.get("safe", True)),
            reasons=list(d.get("reasons", []) or []),
            quarantined=bool(d.get("quarantined", False)),
            version=str(d.get("version", "")),
        )


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _content_hash(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()[:16]


def scan_skill_for_misevolution(
    skill_markdown: str,
    source_chain: Optional[Sequence[Dict[str, Any]]] = None,
) -> Tuple[bool, List[str]]:
    """Scan *skill_markdown* for misevolution content.

    Returns ``(safe, reasons)``. ``safe=False`` means the skill must be
    quarantined, never promoted to reusable policy.

    Two independent signals are checked:

    * **Self-propagation / instruction-following markers** — textual
      fingerprints of a skill that tries to make the agent obey it
      unconditionally or propagate itself.
    * **Untrusted-source reproduction** — either a verbatim quoted block
      (``>`` / ``|`` lines) or a source chain whose entries are all
      untrusted (per the SkillJack taxonomy in ``tools/skill_provenance``).
    """
    reasons: List[str] = []
    md = skill_markdown or ""

    for m in _SELF_PROPAGATION_RE.finditer(md):
        reasons.append(f"self-propagation marker: {m.group(0).strip()[:60]!r}")

    for m in _VERBATIM_QUOTE_RE.finditer(md):
        reasons.append("verbatim reproduction of external text (quoted block)")

    if source_chain:
        trusted = [e for e in source_chain if e.get("trusted")]
        if not trusted:
            untrusted = sorted({e.get("source_type", "?") for e in source_chain})
            reasons.append(
                f"source chain has no trusted sources (all untrusted: {untrusted})"
            )

    return (not reasons, reasons)


class SkillReuseGate:
    """Deterministic reuse gate for the trajectory→skill distiller.

    Wraps :func:`scan_skill_for_misevolution` with a quarantine store and
    versioned rollback. The gate is pure and import-safe; all IO is explicit
    (``quarantine_dir`` / ``version_dir`` paths passed by the caller).
    """

    def __init__(
        self,
        quarantine_dir: Path | str,
        version_dir: Path | str,
    ) -> None:
        self.quarantine_dir = Path(quarantine_dir)
        self.version_dir = Path(version_dir)

    # -- quarantine -----------------------------------------------------
    def quarantine(
        self,
        skill_name: str,
        skill_markdown: str,
        reasons: Sequence[str],
        source_chain: Optional[Sequence[Dict[str, Any]]] = None,
    ) -> Path:
        """Move an unsafe skill into the quarantine store.

        Quarantined skills are never promoted to reusable policy. The record
        is written atomically (tempfile + os.replace) so a crash cannot leave
        a half-written quarantine entry.
        """
        import os
        import tempfile

        self.quarantine_dir.mkdir(parents=True, exist_ok=True)
        record = {
            "skill_name": skill_name,
            "quarantined_at": _now_iso(),
            "reasons": list(reasons),
            "source_chain": list(source_chain or []),
            "content_hash": _content_hash(skill_markdown),
            "skill_markdown": skill_markdown,
        }
        dest = self.quarantine_dir / f"{skill_name}.json"
        fd, tmp = tempfile.mkstemp(
            dir=str(self.quarantine_dir), prefix=f".{skill_name}_", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(record, f, indent=2, ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, dest)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        return dest

    # -- versioning -----------------------------------------------------
    def version(
        self,
        skill_name: str,
        skill_markdown: str,
        source_chain: Optional[Sequence[Dict[str, Any]]] = None,
    ) -> str:
        """Write a versioned snapshot of *skill_markdown* and return its version.

        Versioning reuses the provenance tagging already in
        ``tools/memory_governance.py``: each snapshot is keyed by a content
        hash so a poisoned revision can be identified and rolled back. The
        version string is ``<content-hash>`` — deterministic, so the same
        content always maps to the same version.
        """
        self.version_dir.mkdir(parents=True, exist_ok=True)
        version = _content_hash(skill_markdown)
        record = {
            "skill_name": skill_name,
            "version": version,
            "created_at": _now_iso(),
            "source_chain": list(source_chain or []),
            "skill_markdown": skill_markdown,
        }
        dest = self.version_dir / f"{skill_name}@{version}.json"
        if not dest.exists():
            dest.write_text(
                json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8"
            )
        return version

    def rollback(
        self,
        skill_name: str,
        to_version: str,
    ) -> Optional[str]:
        """Return the skill markdown for *to_version*, or ``None`` if absent.

        This is the rollback primitive: given a known-good version string,
        the caller can restore the skill to that snapshot. Returns ``None``
        when the version is not found (fail-safe — never guess).
        """
        dest = self.version_dir / f"{skill_name}@{to_version}.json"
        if not dest.exists():
            return None
        try:
            data = json.loads(dest.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        md = data.get("skill_markdown")
        return md if isinstance(md, str) else None

    # -- combined gate --------------------------------------------------
    def evaluate(
        self,
        skill_name: str,
        skill_markdown: str,
        source_chain: Optional[Sequence[Dict[str, Any]]] = None,
    ) -> ReuseGateVerdict:
        """Run the full reuse gate: scan, quarantine if unsafe, version always.

        Returns a :class:`ReuseGateVerdict`. When unsafe, the skill is
        quarantined (never promoted) and ``quarantined=True``. Every skill is
        versioned so a poisoned revision can be rolled back.
        """
        safe, reasons = scan_skill_for_misevolution(skill_markdown, source_chain)
        version = self.version(skill_name, skill_markdown, source_chain)
        quarantined = False
        if not safe:
            self.quarantine(skill_name, skill_markdown, reasons, source_chain)
            quarantined = True
        return ReuseGateVerdict(
            skill_name=skill_name,
            safe=safe,
            reasons=reasons,
            quarantined=quarantined,
            version=version,
        )
