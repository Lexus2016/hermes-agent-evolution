"""Memory governance: provenance-gated recall + temporal supersession (#2437).

Two mechanisms that stop stale-gate re-wakes and memory poisoning:

1. **Temporal supersession** — an entry can be *retired* without being
   destroyed. The bytes stay on disk (audit trail intact), but default recall
   hides the entry. This lets a fact be corrected later in time without the
   model re-waking on the stale version.

2. **Provenance-gated recall** — recall of a superseded entry is only granted
   when the caller explicitly opts in with ``include_superseded=True``, and
   every supersession records *who/when* so the audit is attribution-capable
   rather than a bare delete.

On-disk representation
----------------------

The supersession marker is an OUTERMOST trailer, so it parses *before* the
provenance trailer (``⟦src:…|trust:…⟧``) that ``memory_tool`` already appends.
Layout of a superseded entry::

    <display text> ⟦sup:2026-08-15T11:23:00Z⟧ ⟦src:…|trust:…⟧

Parsing order matters for security: ``parse_supersession`` runs first on the
raw entry, then ``memory_tool.parse_provenance`` is fed the *display* text.
A malformed / mismatched marker degrades to "not superseded" (``None``) and the
entry is treated as ordinary content — we never infer governance state from
garbage, mirroring ``parse_provenance``'s fail-safe contract.

Imports are lazy here and in ``memory_tool`` so neither module grows a hard
import-cycle: this module imports only from ``memory_tool`` (for the
*constants* it shares), while ``memory_tool`` imports this module's functions
at the single call site.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

# OUTERMOST supersession trailer. The ``sup:`` prefix is namespaced so it
# cannot collide with a provenance marker; the ISO-8601 UTC timestamp is the
# immutable audit record (first supersession wins).
_SUP_OPEN = "⟦sup:"
_SUP_CLOSE = "⟧"
_SUP_RE = re.compile(r"⟦sup:(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z)⟧\s*$")


def _now_iso() -> str:
    """Current UTC timestamp in the canonical supersession format."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def encode_supersession(text: str, timestamp: Optional[str] = None) -> str:
    """Return ``text`` with a supersession trailer appended.

    ``timestamp`` defaults to now (UTC). Passing an explicit timestamp makes
    the operation reproducible in tests. A malformed timestamp is stored
    verbatim rather than guessed (``parse_supersession`` will then treat the
    entry as un-superseded, which is the safe direction).
    """
    text = (text or "").strip()
    ts = timestamp if timestamp is not None else _now_iso()
    return f"{text} {_SUP_OPEN}{ts}{_SUP_CLOSE}"


def parse_supersession(stored: str) -> Tuple[str, Optional[str]]:
    """Split ``stored`` into ``(display_text, superseded_at)``.

    Returns ``(stored, None)`` when the outermost trailer is absent or
    malformed. The returned ``display_text`` still carries any provenance
    trailer; the caller must run ``memory_tool.parse_provenance`` on it next.
    """
    s = (stored or "").rstrip()
    m = _SUP_RE.search(s)
    if m is None:
        return stored, None
    return s[: m.start()].rstrip(), m.group(1)


def supersede_entry(
    entries: List[str], old_text: str, timestamp: Optional[str] = None
) -> Dict[str, Any]:
    """Mark the entry containing *old_text* as superseded, in place.

    Operates on the raw on-disk entry list. First supersession wins: if the
    matched entry is already superseded, no timestamp is changed (the original
    audit record is preserved) and ``already_superseded`` is set.

    Returns a result dict; on success the entry list is mutated in place.
    """
    old_text = (old_text or "").strip()
    if not old_text:
        return {"success": False, "error": "old_text cannot be empty."}

    for i, entry in enumerate(entries):
        text, _sup = parse_supersession(entry)
        if old_text in text:
            if _sup is not None:
                return {
                    "success": True,
                    "already_superseded": True,
                    "superseded_at": _sup,
                }
            entries[i] = encode_supersession(text, timestamp)
            return {
                "success": True,
                "already_superseded": False,
                "superseded_at": timestamp if timestamp is not None else _now_iso(),
            }

    return {"success": False, "error": f"No entry matching '{old_text}' found."}


def governed_search(
    store: Any,
    target: str,
    source_filter: Optional[object] = None,
    min_trust: Optional[str] = None,
    include_superseded: bool = False,
) -> List[Dict[str, Any]]:
    """Supersession-aware wrapper around ``store.search``.

    Default (``include_superseded=False``) hides superseded entries entirely.
    When ``include_superseded=True`` every returned row carries a
    ``superseded_at`` field (``None`` for live entries) so the caller can
    distinguish live vs. retired facts. For stores with no superseded entries
    the default path is byte-identical to a plain ``store.search`` call.
    """
    # ``store.search`` already understands the supersession marker natively
    # (its ``include_superseded`` kwarg hides superseded entries and, when
    # True, attaches a ``superseded_at`` field). Delegate directly — this
    # wrapper only exists so non-``MemoryStore`` callers inherit the same
    # provenance-gated recall contract without reaching into memory internals.
    return store.search(
        target,
        source_filter=source_filter,
        min_trust=min_trust,
        include_superseded=include_superseded,
    )
