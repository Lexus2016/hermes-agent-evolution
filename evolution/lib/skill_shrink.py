# -*- coding: utf-8 -*-
"""SkillProx Slice 3 — retroactive proximal shrinkage (#2779).

Child of #2744. Verification can also run BACKWARD over already-published
skill content: a section whose measured utility is negative (it failed the
re-execution check every time it ran) is removed from the live body, with
the prior body retained in the shrink history for audit/restore.

Proximal by construction: the only inputs are the CURRENT body and the
verdict memory the S2 store already keeps — no retraining, no LLM, no
scan of anything but the sections named in the verdict store.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from evolution.lib.skill_prox import _default_store_path, _load_verdicts

logger = logging.getLogger(__name__)

__all__ = ["ShrinkResult", "shrink_negative_sections", "SHRINK_MARKER"]


@dataclass
class ShrinkResult:
    """Outcome of one retroactive shrink pass."""

    shrunk: bool
    new_body: str = ""
    removed_sections: List[str] = field(default_factory=list)
    history_path: Optional[Path] = None


def _iter_sections(body: str):
    """Yield (section_header, section_text_including_header) top-level MD."""
    lines = body.splitlines(keepends=True)
    start = 0
    header = None
    for i, line in enumerate(lines):
        if line.startswith("## "):
            if header is not None:
                yield header, "".join(lines[start:i])
            header, start = line.strip(), i
    if header is not None:
        yield header, "".join(lines[start:])


def _negative_section_keys(
    skill_name: str, body: str, store_path: Optional[Path]
) -> List[str]:
    """Sections whose EXACT current text is recorded as rejected."""
    verdicts = _load_verdicts(store_path or _default_store_path())
    return [
        header
        for header, text in _iter_sections(body)
        if verdicts.get(_section_key(skill_name, header, text)) is False
    ]


def _section_key(skill_name: str, header: str, text: str) -> str:
    """Verdict-store identity of one section occurrence (skill+header+text)."""
    import hashlib

    return hashlib.sha256(
        f"{(skill_name or '').strip()}\n{header}\n{text}".encode("utf-8")
    ).hexdigest()


def shrink_negative_sections(
    skill_name: str,
    body: str,
    *,
    store_path: Optional[Path] = None,
    history_dir: Optional[Path] = None,
) -> ShrinkResult:
    """Remove sections with recorded negative utility (#2779).

    A section is negative-utility when its exact current text is recorded as
    REJECTED in the S2 verdict store (the re-execution check failed on it).
    The prior body is archived to ``<history_dir or store parent>/shrink-
    history/<skill>.jsonl`` before the shrink so the removal is auditable
    and reversible.
    """
    negative = _negative_section_keys(skill_name, body, store_path)
    if not negative:
        return ShrinkResult(shrunk=False, new_body=body)

    removed_texts: Dict[str, str] = {}
    kept: List[str] = []
    for header, text in _iter_sections(body):
        if header in negative:
            removed_texts[header] = text
        else:
            kept.append(text)

    new_body = "".join(kept).rstrip() + "\n"
    hist_dir = history_dir or (store_path or _default_store_path()).parent / "shrink-history"
    hist_path: Optional[Path] = None
    try:
        import json
        import time as _time

        hist_dir.mkdir(parents=True, exist_ok=True)
        hist_path = hist_dir / f"{(skill_name or 'skill').replace('/', '_')}.jsonl"
        rec = {
            "removed_sections": sorted(removed_texts),
            "prior_body": body,
            "shrunk_at": _time.strftime("%Y-%m-%dT%H:%M:%SZ", _time.gmtime()),
        }
        with hist_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, sort_keys=True) + "\n")
    except OSError as exc:
        logger.debug("shrink-history write failed: %s", exc)
        hist_path = None

    return ShrinkResult(
        shrunk=True,
        new_body=new_body,
        removed_sections=sorted(removed_texts),
        history_path=hist_path,
    )
