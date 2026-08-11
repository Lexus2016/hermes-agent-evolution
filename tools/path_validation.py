"""Shared pure-Python path validation + nearby-file surfacing.

Foundation for the cross-tool file-not-found fix (#2242). Unlike the
shell-based ``_suggest_similar_files`` in ``file_operations.py``, this
module uses plain ``os``/``pathlib`` so any tool (read_file, terminal,
search_files, patch) can reuse it without a shell backend. Slice A (#2293)
integrates it into read_file; Slice B extends it to the other three tools.
"""

import os
from pathlib import Path
from typing import List, Optional


def suggest_nearby_paths(path: str, max_results: int = 5) -> List[str]:
    """Return up to *max_results* paths near *path* the caller likely meant.

    Pure-Python existence check + similarity scoring (no shell). Returns
    ``[]`` if *path* exists. Otherwise scores parent-dir entries by name
    similarity to the requested basename; if the parent doesn't exist, walks
    up to the nearest existing ancestor and lists its entries.
    """
    p = Path(path)
    if p.exists():
        return []
    dir_path, filename = p.parent, p.name
    base_no_ext, ext = p.stem, p.suffix.lower()
    lower = filename.lower()

    entries: List[str] = []
    target = dir_path
    while True:
        try:
            entries = sorted(os.listdir(target))
            break
        except OSError:
            parent = target.parent
            if parent == target:
                break
            target = parent

    scored: List[tuple] = []
    for f in entries:
        lf = f.lower()
        score = 0
        if lf == lower:
            score = 100
        elif os.path.splitext(f)[0].lower() == base_no_ext.lower():
            score = 90
        elif lf.startswith(lower) or lower.startswith(lf):
            score = 70
        elif lower in lf:
            score = 60
        elif lf in lower and len(lf) > 2:
            score = 40
        elif ext and os.path.splitext(f)[1].lower() == ext:
            common = set(lower) & set(lf)
            if len(common) >= max(len(lower), len(lf)) * 0.4:
                score = 30
        if score > 0:
            scored.append((score, os.path.join(target, f)))

    scored.sort(key=lambda x: -x[0])
    return [fp for _, fp in scored[:max_results]]


def format_nearby_hint(path: str, nearby: List[str]) -> Optional[str]:
    """Build a human hint for a stale *path* given *nearby* candidates.

    Returns ``None`` when *nearby* is empty. Mirrors the #1587 "did you
    mean?" inline-suggestion contract so the hint is visible in the tool
    error text, not a separate field.
    """
    if not nearby:
        return None
    return (
        f"File not found: {path}\n\n"
        f"Did you mean: {', '.join(nearby)}? "
        "Re-run read_file with one of these paths instead of guessing."
    )
