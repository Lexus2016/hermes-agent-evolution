"""
Memory governance: temporal supersession + provenance-gated recall — #2437.

The evolution pipeline's memory layer recalls any note forever, regardless
of whether it has been superseded, and recall does not check provenance.
Two failure classes trace directly to this: stale-gate re-wakes (an
orchestrator re-dispatches a stage because a "done" note was superseded by
a newer "pending" one, but the stale note is still recalled) and memory
poisoning (an untrusted note overrides a trusted one and is recalled with
equal standing).

This module implements the governance mechanics as a standalone codec +
recall wrapper so it is testable and usable without touching the
``MemoryStore`` internals:

* **Temporal supersession** — a superseded entry gains a visible trailer
  ``⟦sup:<ISO-8601-UTC>|status:superseded⟧`` appended AFTER any #316
  provenance trailer. Bytes stay on disk for audit; recall hides the entry
  unless explicitly asked for.
* **Provenance-gated recall** — ``governed_search`` filters rows by source
  class / minimum trust tier AND by supersession state, with superseded
  entries excluded by default.

Parsing order matters: supersession is the OUTERMOST trailer (appended
last), so ``parse_supersession`` must run FIRST; the remaining display
text is then fed to ``tools.memory_tool.parse_provenance``. Feeding a
combined entry to ``parse_provenance`` alone degrades to safe defaults
(its ``|trust:`` split would swallow the sup trailer into the tier field
and fail the vocabulary check) — which is exactly the safe-direction
failure: no governance state is ever inferred from a malformed stack.

Backward compatibility mirrors #316: legacy entries parse as active;
default recall behaviour is unchanged for stores without superseded
entries; malformed trailers are ordinary content.
"""

from __future__ import annotations

import datetime as _datetime
from typing import Any, Dict, Iterable, List, Optional

__all__ = [
    "encode_supersession",
    "parse_supersession",
    "is_superseded",
    "utc_now_iso",
    "supersede_entry",
    "governed_search",
    "SUP_STATUS",
    "SUP_OPEN",
    "SUP_CLOSE",
]

# Trailer sentinels — kept as module literals so encode/parse share one source.
SUP_OPEN = "⟦sup:"
SUP_CLOSE = "⟧"
SUP_STATUS = "superseded"


def utc_now_iso() -> str:
    """Current UTC time as a second-resolution ISO-8601 string (``...Z``)."""
    return (
        _datetime.datetime.now(_datetime.timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def parse_supersession(stored: str):
    """Split a stored entry into ``(display_text, superseded_at)``.

    ``superseded_at`` is ``None`` for active entries — legacy entries,
    default adds, or anything without a well-formed trailer. A malformed
    trailer stays part of the display text: governance state is never
    inferred from garbage (same contract as ``parse_provenance``).
    """
    s = stored.rstrip()
    if not s.endswith(SUP_CLOSE):
        return stored, None
    open_at = s.rfind(SUP_OPEN)
    if open_at == -1:
        return stored, None
    inner = s[open_at + len(SUP_OPEN) : -len(SUP_CLOSE)]
    # inner looks like "<iso-ts>|status:superseded"
    if "|status:" not in inner:
        return stored, None
    ts, status = inner.split("|status:", 1)
    if status != SUP_STATUS or not ts:
        return stored, None
    display = s[:open_at].rstrip()
    return display, ts


def encode_supersession(stored: str, superseded_at: str) -> str:
    """Return ``stored`` with a supersession trailer appended.

    Idempotent: an entry that already carries a well-formed trailer is
    returned unchanged — the FIRST supersession timestamp is the audit
    record and is never rewritten.
    """
    if parse_supersession(stored)[1] is not None:
        return stored
    return f"{stored.rstrip()} {SUP_OPEN}{superseded_at}|status:{SUP_STATUS}{SUP_CLOSE}"


def is_superseded(stored: str) -> bool:
    """True when the stored entry carries a well-formed supersession trailer."""
    return parse_supersession(stored)[1] is not None


def supersede_entry(
    entries: List[str],
    match: str,
    superseded_at: Optional[str] = None,
) -> Dict[str, Any]:
    """Supersede the first entry whose display text contains ``match``.

    Pure-list helper (no store I/O) so callers can apply it inside their
    own lock/critical section. Returns a result dict:

    * success + ``superseded_at`` + index on success
    * success + ``already_superseded`` when the match is already retired
      (idempotent no-op)
    * error ``no match`` / ``ambiguous match`` (multiple distinct entries
      contain ``match``) — mirroring MemoryStore's replace/remove contract
    """
    match = (match or "").strip()
    if not match:
        return {"success": False, "error": "match cannot be empty."}
    ts = superseded_at or utc_now_iso()
    matches = [i for i, e in enumerate(entries) if match in parse_supersession(e)[0]]
    if not matches:
        return {
            "success": False,
            "error": f"No entry matched '{match}'.",
        }
    if len({entries[i] for i in matches}) > 1:
        return {
            "success": False,
            "error": f"Multiple distinct entries matched '{match}'. Be more specific.",
        }
    idx = matches[0]
    if is_superseded(entries[idx]):
        return {"success": True, "already_superseded": True, "index": idx}
    entries[idx] = encode_supersession(entries[idx], ts)
    return {"success": True, "superseded_at": ts, "index": idx}


def governed_search(
    store,
    target: str,
    *,
    source_filter: Optional[Iterable[str]] = None,
    min_trust: Optional[str] = None,
    include_superseded: bool = False,
) -> List[Dict[str, Any]]:
    """Provenance-gated, supersession-aware recall over any MemoryStore-like object.

    Wraps ``store.search(target, source_filter=…, min_trust=…)`` (the #316
    retrieval path) and layers temporal governance on top:

    * superseded entries are excluded unless ``include_superseded=True``
    * when included, each row carries ``"superseded_at": <ts>`` so the
      caller can see it is retired
    * provenance filters pass straight through to the store

    Rows are ``{"text", "source_class", "trust_tier"[, "superseded_at"]}``.
    Raises nothing the underlying ``store.search`` wouldn't; the #316
    search path is read-only and non-raising.
    """
    rows = store.search(target, source_filter=source_filter, min_trust=min_trust)
    out: List[Dict[str, Any]] = []
    for row in rows:
        # store.search returns display text with the stored trailer already
        # stripped via parse_provenance — but the sup trailer rides INSIDE
        # that text (memory_tool does not know about it until the #2437
        # integration patch lands). Detect it on the row text; when the
        # native integration lands, rows come pre-split and this still
        # works (no trailer → active entry).
        text = row.get("text", "")
        display, sup_ts = parse_supersession(text)
        if sup_ts is not None and not include_superseded:
            continue
        row = dict(row)
        row["text"] = display
        if sup_ts is not None:
            row["superseded_at"] = sup_ts
        out.append(row)
    return out
