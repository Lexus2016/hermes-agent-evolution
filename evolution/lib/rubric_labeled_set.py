# -*- coding: utf-8 -*-
"""RubricForge Slice 2 — labeled-set collection (#2781).

Child of #2760. Persists ground-truth labeled examples for the Slice-1
agreement primitive (#2797): the store IS the file the rubric judge's
consumer reads (``rubric-forge/labeled.json`` — a JSON array of
``{"requires": [...], "forbids": [...], "label": bool}`` records).

Ground truth is LABELED by definition — sources are owner verdicts,
postmortem outcomes, or curated review notes; this module owns only the
collection mechanics:

- ``append_labeled_examples()`` — validate, dedup (canonical-JSON key), and
  merge new examples into the store; bounded (default keeps the newest 200)
  so the agreement pass stays cheap.
- ``default_store_path()`` — $EVOLUTION_PROFILE_DIR/rubric-forge/labeled.json
  (the exact path ``evolution_rubric_judge.resolve_active_rubric`` reads).
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

logger = logging.getLogger(__name__)

__all__ = ["append_labeled_examples", "default_store_path", "load_labeled_set"]

_MAX_EXAMPLES = 200


def default_store_path() -> Path:
    """The store the judge's RubricForge consumer reads (S1 wiring)."""
    base = (
        os.environ.get("EVOLUTION_PROFILE_DIR", "").strip()
        or str(Path.home() / ".hermes" / "evolution")
    )
    return Path(base) / "rubric-forge" / "labeled.json"


def _validate(example: Any) -> Optional[Dict[str, Any]]:
    """Normalize one candidate example; None when it carries no signal."""
    if not isinstance(example, dict):
        return None
    requires = [str(k) for k in (example.get("requires") or []) if str(k)]
    forbids = [str(k) for k in (example.get("forbids") or []) if str(k)]
    if not requires and not forbids:
        return None
    return {"requires": requires, "forbids": forbids, "label": bool(example.get("label"))}


def _key(example: Dict[str, Any]) -> str:
    return json.dumps(example, sort_keys=True)


def load_labeled_set(store_path: Optional[Path] = None) -> List[Dict[str, Any]]:
    """Read the store; a missing/corrupt file is an empty set (never raises)."""
    try:
        data = json.loads((store_path or default_store_path()).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [e for e in data if isinstance(e, dict)] if isinstance(data, list) else []


def append_labeled_examples(
    examples: Sequence[Any],
    *,
    store_path: Optional[Path] = None,
    max_examples: int = _MAX_EXAMPLES,
) -> int:
    """Merge validated examples into the store; returns how many were added.

    Duplicates (canonical-JSON identity, including label) are skipped. The
    store keeps the NEWEST ``max_examples`` after the merge. Best-effort:
    an unwritable store logs and returns 0.
    """
    path = store_path or default_store_path()
    existing = load_labeled_set(path)
    seen = {_key(e) for e in existing}
    added = 0
    for candidate in examples:
        normalized = _validate(candidate)
        if normalized is None or _key(normalized) in seen:
            continue
        existing.append(normalized)
        seen.add(_key(normalized))
        added += 1
    if not added:
        return 0
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(existing[-max_examples:], indent=2) + "\n", encoding="utf-8"
        )
    except OSError as exc:
        logger.debug("labeled-set write failed: %s", exc)
        return 0
    return added
