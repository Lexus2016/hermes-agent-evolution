"""MemoryStore — bounded, file-backed curated memory (MEMORY.md / USER.md).
Entries are joined by ``ENTRY_DELIMITER``; budgets are in chars (model-independent).
Module state that tests monkeypatch (``get_memory_dir``, ``fcntl``/``msvcrt``) stays
in ``tools.memory_tool`` and is read lazily."""

import copy
import hashlib
import json
import logging
import os
import re
import time
from contextlib import contextmanager, suppress
from contextvars import ContextVar
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from hermes_constants import get_hermes_home
from utils import atomic_write_text, is_truthy_value
from tools.threat_patterns import first_threat_message as _first_threat_message
from tools.threat_patterns import scan_for_threats
from tools.memory_governance import parse_supersession, supersede_entry

msvcrt = None
try:
    import fcntl
except ImportError:
    fcntl = None
    try:
        import msvcrt
    except ImportError:
        pass

logger = logging.getLogger("tools.memory_tool")

def get_memory_dir() -> Path:
    try:
        from tools import memory_tool
        return memory_tool.get_memory_dir()
    except Exception:
        return get_hermes_home() / "memories"

MEMORY_BLOCK_HEADERS = {
    "memory": "MEMORY (your personal notes)",
    "user": "USER PROFILE (who the user is)",
}

ENTRY_DELIMITER = "\n§\n"

# Sidecar recording threat-blocked memory entries we have already warned about.
# Keyed by "<filename>|<sha256(entry)>" so a poisoned-on-disk entry warns ONCE
# per unique text, not once per session start (#138). The raw entry
# intentionally stays on disk (live state keeps it visible and removable);
# only the WARNING is deduped — the [BLOCKED:] snapshot replacement is
# unaffected and still happens on every load.
BLOCKED_WARNINGS_FILE = ".memory_blocked_warnings.json"
# Bounded so a long-lived profile cannot grow the sidecar without bound.
# Dropping an old fingerprint only ever costs one re-warning for that text.
BLOCKED_WARNINGS_MAX = 512


# ---------------------------------------------------------------------------
# Source-provenance tagging (issue #316)
#
# Every memory entry can carry a *source class* (who/what produced it) and a
# *trust tier* (how much to trust it). This is the first slice of the
# memory-poisoning-guard epic (#315): here we only RECORD provenance and let
# retrieval FILTER on it. The block/warn/strip enforcement lives in #315.
#
# Backward-compatibility is the hard constraint:
#   * Old §-delimited files predate provenance. Their entries are plain
#     strings with no trailer; they parse to the safe defaults below.
#   * A *default* add (no explicit provenance) writes the entry verbatim —
#     NO trailer — so on-disk bytes and no-filter retrieval stay identical to
#     pre-#316 behaviour and the external-drift round-trip check is unaffected.
#   * Only when explicit provenance is supplied do we append a single visible
#     trailer to the entry string. The trailer is part of the stored string,
#     so disk serialization stays ``ENTRY_DELIMITER.join(strings)`` and old
#     readers treat it as ordinary entry text rather than choking on it.
#
# Trailer format (appended to the entry text, separated by a single space):
#     ⟦src:<source_class>|trust:<trust_tier>⟧
# The brackets U+27E6/U+27E7 are visible (not invisible-unicode) so the threat
# scanner does not flag them, and they are vanishingly unlikely to collide
# with real entry content.
# ---------------------------------------------------------------------------

# source classes; "unknown" is the safe fallback for any entry whose origin we
# cannot establish (e.g. legacy files).
SOURCE_CLASSES = (
    "user_input",
    "external_tool",
    "agent_authored",
    "system",
    "unknown",
)

# trust tiers ordered LOW -> HIGH so ``min_trust`` is a simple index compare.
# "unknown" sits at the bottom: an untagged legacy entry must never clear a
# trust bar it was never evaluated against.
TRUST_TIERS = ("unknown", "untrusted", "low", "medium", "trusted")

DEFAULT_SOURCE_CLASS = "unknown"
DEFAULT_TRUST_TIER = "unknown"

# Sentinels — kept as literals so parse/encode share one source.
_PROV_OPEN = "⟦src:"
_PROV_CLOSE = "⟧"


def _trust_rank(tier: str) -> int:
    """Return the ordering rank of a trust tier (unknown lowest). -1 if invalid."""
    try:
        return TRUST_TIERS.index(tier)
    except ValueError:
        return -1


def encode_provenance(text: str, source_class: str, trust_tier: str) -> str:
    """Return the on-disk string for ``text`` with a provenance trailer.

    When ``source_class`` and ``trust_tier`` are BOTH the safe defaults, the
    text is returned unchanged (no trailer) so default adds stay byte-identical
    to the pre-#316 format. Otherwise a single ``⟦src:…|trust:…⟧`` trailer is
    appended, separated by one space.
    """
    text = text.strip()
    if source_class == DEFAULT_SOURCE_CLASS and trust_tier == DEFAULT_TRUST_TIER:
        return text
    return f"{text} {_PROV_OPEN}{source_class}|trust:{trust_tier}{_PROV_CLOSE}"


def parse_provenance(stored: str):
    """Split a stored entry into ``(display_text, source_class, trust_tier)``.

    Entries written before #316 (and default adds) have no trailer, so they
    parse to ``(stored, DEFAULT_SOURCE_CLASS, DEFAULT_TRUST_TIER)``. A trailing
    ``⟦src:<class>|trust:<tier>⟧`` token, if present and well-formed with a
    recognised class+tier, is stripped from the display text and returned as
    the provenance. A malformed or unrecognised trailer is left as part of the
    text (treated as ordinary content) and defaults are returned — we never
    guess provenance from garbage.
    """
    s = stored.rstrip()
    if not s.endswith(_PROV_CLOSE):
        return stored, DEFAULT_SOURCE_CLASS, DEFAULT_TRUST_TIER
    open_at = s.rfind(_PROV_OPEN)
    if open_at == -1:
        return stored, DEFAULT_SOURCE_CLASS, DEFAULT_TRUST_TIER
    inner = s[open_at + len(_PROV_OPEN) : -len(_PROV_CLOSE)]
    # inner looks like "<source_class>|trust:<trust_tier>"
    if "|trust:" not in inner:
        return stored, DEFAULT_SOURCE_CLASS, DEFAULT_TRUST_TIER
    src, tier = inner.split("|trust:", 1)
    if src not in SOURCE_CLASSES or tier not in TRUST_TIERS:
        # Unrecognised vocabulary — treat the whole thing as plain content.
        return stored, DEFAULT_SOURCE_CLASS, DEFAULT_TRUST_TIER
    display = s[:open_at].rstrip()
    return display, src, tier


def _entry_grounding(stored: str) -> tuple[str, str, str, str]:
    """Resolve context metadata; neither model tags nor storage approval grant authority.

    There is no authenticated arrangement-acceptance receipt in the memory
    format. Legacy/default entries therefore remain unacknowledged, even when
    their source/trust labels claim user input or high reliability.
    """
    entry, superseded_at = parse_supersession(stored)
    text, source, trust = parse_provenance(entry)
    return text, source, trust, "revised" if superseded_at is not None else "unacknowledged"


# ---------------------------------------------------------------------------
# Memory content scanning — lightweight check for injection/exfiltration
# in content that gets injected into the system prompt.
#
# Patterns live in ``tools/threat_patterns.py`` — the single source of truth
# shared with the context-file scanner and the tool-result delimiter system.
# Memory uses the "strict" scope (broadest pattern set) because:
#  - memory entries are user-curated; the user can rewrite a flagged entry
#  - memory enters the system prompt as a FROZEN snapshot, so a poisoned
#    entry persists for the entire session and across sessions until
#    explicitly removed.
# ---------------------------------------------------------------------------

from tools.threat_patterns import first_threat_message as _first_threat_message

from tools.memory_governance import (
    parse_supersession,
    supersede_entry,
)


def _scan_memory_content(content: str) -> Optional[str]:
    """Scan memory content for injection/exfil patterns. Returns error string if blocked."""
    return _first_threat_message(content, scope="strict")


# Why the last refusal/failure in this context happened, for the shared metric only (a closed name from
# shared_metrics_contract.MEMORY_OP_FAILURE_CLASSES); never part of the result the model sees.
FAILURE_CLASS: ContextVar[str] = ContextVar("memory_failure_class", default="other")


def _error(message: str, failure_class: str, **extra) -> dict[str, Any]:
    FAILURE_CLASS.set(failure_class)
    return {"success": False, "error": message, **extra}


def _store_target(filename: str) -> str:
    """The memory tool target that owns a store file, for hints that show the call to make."""
    return "user" if filename == "USER.md" else "memory"


# Typography a model re-types differently from the stored entry: every quote/backtick -> "'",
# every dash/minus -> "-". Plus whitespace runs (re-wrapped lines, NBSP) -> one space.
_MATCH_FOLD = str.maketrans({**dict.fromkeys("\"`\u2018\u2019\u201a\u201b\u201c\u201d\u201e\u201f", "'"),
                             **dict.fromkeys("\u2010\u2011\u2012\u2013\u2014\u2015\u2212", "-")})


def _normalize_for_match(text: str) -> str:
    """Quote/dash/whitespace-folded form for tolerant old_text matching. Models re-emit an
    entry with straight quotes for curly ones, '-' for an em dash, unwrapped lines, or a
    newline over-escaped to a literal backslash-n, and a byte-exact substring test then
    reports "No entry matched" for an unambiguous target."""
    return " ".join(str(text).replace("\\n", " ").translate(_MATCH_FOLD).split())


def _substring_matches(entries: list[str], old_text: str) -> list[int]:
    """Indices of entries containing *old_text*: byte-exact first, else typography-folded."""
    if exact := [i for i, e in enumerate(entries) if old_text in e]:
        return exact
    needle = _normalize_for_match(old_text)
    if not any(c.isalnum() for c in needle):  # punctuation/space alone would select a whole entry
        return []
    return [i for i, e in enumerate(entries) if needle in _normalize_for_match(e)]


def _make_provenance(source_class: str, trust_tier: str):
    """Build a guard ``Provenance`` from the entry's source class + trust tier.

    Imported lazily so ``tools.memory_tool`` keeps no hard dependency on the
    optional guard module (the default-off path never touches it).
    """
    from agent.memory_guard import Provenance

    return Provenance(source_class=source_class, trust_tier=trust_tier)


def _log_guard_event(action: str, target: str, event: dict[str, Any]) -> None:
    """Emit a structured guard event to the logger (warn/strip decisions).

    Block decisions surface to the model via the tool error already; warn/strip
    allow the write to proceed, so we log them here so the decision is visible in
    the trace (issue #315 success criterion: "policy violations produce
    structured guard events").
    """
    logger.warning(
        "memory guard event: op=%s target=%s %s",
        action,
        target,
        json.dumps(event, ensure_ascii=False),
    )


def _validate_provenance(source_class: str, trust_tier: str) -> Optional[str]:
    """Return an error string if provenance values are out of vocabulary, else None."""
    if source_class not in SOURCE_CLASSES:
        return (
            f"Invalid source_class '{source_class}'. "
            f"Use one of: {', '.join(SOURCE_CLASSES)}."
        )
    if trust_tier not in TRUST_TIERS:
        return (
            f"Invalid trust_tier '{trust_tier}'. Use one of: {', '.join(TRUST_TIERS)}."
        )
    return None


def _drift_error(path: "Path", bak_path: str) -> dict[str, Any]:
    """Build the error dict returned when external drift is detected.

    The on-disk memory file contains content that wouldn't round-trip
    through the tool's parser/serializer — flushing would discard the
    appended/edited content from a patch tool, shell append, manual edit,
    or sister-session write. We refuse the mutation, point the operator at
    the .bak.<ts> snapshot we took, and tell them what to do next.
    """
    FAILURE_CLASS.set("drift")
    return {
        "success": False,
        "error": (
            f"Refusing to write {path.name}: file on disk has content that "
            f"wouldn't round-trip through the memory tool (likely added by "
            f"the patch tool, a shell append, a manual edit, or a "
            f"concurrent session). A snapshot was saved to {bak_path}. "
            f"Resolve the drift first — either rewrite the file as a clean "
            f"§-delimited list of entries, or move the extra content out — "
            f"then retry. This guard exists to prevent silent data loss "
            f"(issue #26045)."
        ),
        "drift_backup": bak_path,
        "remediation": (
            "Open the .bak file, integrate the missing entries into the "
            "memory tool one at a time via memory(action=add, content=...), "
            "then remove or rewrite the original file to a clean state."
        ),
    }


# Sentinel returned by ``_reload_target`` when the target file EXISTS but could
# not be read. Distinct from a drift-backup path (``str``) and from a clean
# reload (``None``): the caller must abort the mutation rather than persist over
# an unreadable file.
_READ_FAILED = object()


def _read_failed_error(path: "Path") -> dict[str, Any]:
    """Build the error dict returned when the on-disk memory file is unreadable.

    A file that exists but cannot be read is NOT an empty store. Reading it as
    ``[]`` and then persisting would rewrite the whole file from an empty entry
    list — wiping the user's memory. We refuse the write so nothing is lost.
    """
    FAILURE_CLASS.set("read_failed")
    return {
        "success": False,
        "error": (
            f"Refusing to write {path.name}: the file exists on disk but could "
            f"not be read right now (temporarily locked by another program, a "
            f"permission change, invalid/corrupt text encoding, or a filesystem "
            f"error). Treating an unreadable file as empty and saving would wipe "
            f"existing memory, so the write is refused. Nothing was changed — "
            f"retry in a moment."
        ),
    }


DEFAULT_MEMORY_CHAR_LIMIT = 2200
DEFAULT_USER_CHAR_LIMIT = 1375

def _find_unique_match(entries: list[str], old_text: str) -> tuple[Optional[int], bool]:
    """``(index, ambiguous)`` for entries matching *old_text*.

    Comparison is on display text (provenance trailer stripped) so a visible
    substring still finds a tagged entry. A whole-entry EXACT display match
    takes priority over substring, so a short entry stays addressable when its
    full text is contained inside a longer sibling. Exact-duplicate stored
    texts are safe (first wins); distinct stored texts → ``(None, True)``.
    """
    displays = [parse_provenance(e)[0] for e in entries]
    exact = [i for i, d in enumerate(displays) if d == old_text]
    matches = exact or _substring_matches(displays, old_text)
    if len({entries[i] for i in matches}) > 1:
        return None, True
    return (matches[0] if matches else None), False


def _no_match_error(entries: list[str], old_text: str, verb: str, **extra) -> dict[str, Any]:
    """Zero-match error that names the fix: the entries sharing the most words with
    *old_text* (a paraphrased or stale old_text usually still shares most of them), and a
    pointer when old_text spans several entries joined by the delimiter."""
    def _words(text: str) -> set:
        return set(re.findall(r"\w+", text.lower()))
    words = _words(old_text)
    scored = sorted(((len(words & _words(e)) / len(words), i) for i, e in enumerate(entries)) if words else [],
                    key=lambda p: (-p[0], p[1]))
    closest = [entries[i] for score, i in scored[:3] if score >= 0.3]
    hint = (" old_text must come from ONE entry ('§' separates entries); use one op per entry."
            if "§" in old_text else "")
    return _error(f"No entry matched '{old_text}'.{hint} Retry the {verb} with old_text copied verbatim "
                  f"from the intended entry" + (" (closest_entries below)." if closest else "."), "no_match",
                  **({"closest_entries": closest} if closest else {}), **extra)


def _over_limit(new_total: int, limit: int) -> str:
    return f"would put memory at {new_total:,}/{limit:,} chars, over the limit: free at least {new_total - limit:,} chars"


# Patch content cannot be empty, and 'remove' would drop the sibling facts in the entry.
PATCH_DROP_HINT = "To drop text inside an entry, patch a longer span that contains it; 'remove' deletes the whole entry."


def _repeated_span_message(old_text: str, count: int) -> str:
    return (f"'{old_text}' appears {count} times in the matched entry, and patch edits one span. "
            f"Include more surrounding text so old_text occurs once.")


def _span_occurrences(text: str, span: str) -> int:
    """Occurrences of *span* in *text*, overlapping ones included: '00' occurs twice in '1000'."""
    return sum(text.startswith(span, i) for i in range(len(text) - len(span) + 1))


def _patch_entry(entry: str, old_text: str, new_text: str) -> tuple[Optional[str], str, str]:
    """``(patched entry, "", "none")``, or ``(None, error, failure class)`` when the span occurs more
    than once in the entry, the edit changes nothing, or the patched entry fails the threat scan.
    Single and batch patches both go through here, so they share one rule."""
    display, source_class, trust_tier = parse_provenance(entry)
    if (count := _span_occurrences(display, old_text)) > 1:
        return None, _repeated_span_message(old_text, count), "ambiguous"
    patched = display.replace(old_text, new_text, 1)
    if patched == display:
        return None, f"Patch is a no-op: '{old_text}' -> '{new_text}' leaves the entry unchanged.", "invalid_args"
    if scan_error := _scan_memory_content(patched):
        return None, scan_error, "scan_blocked"
    return encode_provenance(patched, source_class, trust_tier), "", "none"


def _pinned_index(entries: list[str], matched_entry: str) -> Optional[int]:
    """Index of the exact entry a staged write was reviewed against; None once it is gone (stale)."""
    return entries.index(matched_entry) if matched_entry in entries else None


def _stale_entry_message(entry: str) -> str:
    return (f"Entry changed since it was staged, so this write was not applied: '{entry}' is no longer "
            f"in memory as reviewed. Recreate the change against the current entry or reject it; the "
            f"pending record has been preserved.")


def _locate_stored(entries: list[str], old_text: str, matched_entry: Optional[str] = None):
    """``(index, error_dict)``. Error is set for a stale pin or an ambiguous match.
    Index is None with no error when nothing matched (the caller phrases the miss)."""
    if matched_entry is not None:
        idx = _pinned_index(entries, matched_entry)
        if idx is None:
            FAILURE_CLASS.set("stale_entry")
            return None, {"success": False, "error": _stale_entry_message(matched_entry)}
        return idx, None
    idx, ambiguous = _find_unique_match(entries, old_text)
    if ambiguous:
        FAILURE_CLASS.set("ambiguous")
        displays = [parse_provenance(e)[0] for e in entries]
        previews = [d[:80] + ("..." if len(d) > 80 else "") for d in displays if old_text in d]
        return None, {
            "success": False,
            "error": f"Multiple entries matched '{old_text}'. Be more specific.",
            "matches": previews,
        }
    return idx, None


# Optional third value of an _apply closure: a dict merged into the success payload —
# e.g. the full text an entry-level replace overwrote, so a whole-entry write is never
# silent about the loss (#117952). Same convention as _error(**extra).


class MemoryStore:
    """
    Bounded curated memory with file persistence. One instance per AIAgent.

    Maintains two parallel states:
      - _system_prompt_snapshot: frozen at load time, used for system prompt injection.
        Never mutated mid-session. Keeps prefix cache stable.
      - memory_entries / user_entries: live state, mutated by tool calls, persisted to disk.
        Tool responses always reflect this live state.
    """

    # After this many failed consolidation attempts (overflow / zero-match) in
    # ONE turn, stop instructing the model to "retry in this turn" and return a
    # terminal "save skipped" result so a fragile replace/add can't loop the
    # turn to budget exhaustion and suppress the user's reply (issue #42405).
    _MAX_CONSOLIDATION_FAILURES_PER_TURN = 3

    def __init__(
        self,
        memory_char_limit: int = DEFAULT_MEMORY_CHAR_LIMIT,
        user_char_limit: int = DEFAULT_USER_CHAR_LIMIT,
        *,
        guard: Optional[object] = None,
        allow_batch_override: bool = False,
        auto_evict_on_full: bool = True,
        auto_evict_keep_min: int = 1,
        memory_enabled: bool = True,
        user_profile_enabled: bool = True,
    ):
        self.memory_entries: list[str] = []
        self.user_entries: list[str] = []
        self.memory_char_limit = memory_char_limit
        self.user_char_limit = user_char_limit
        self.memory_enabled = memory_enabled
        self.user_profile_enabled = user_profile_enabled
        # Explicit opt-in for per-call dynamic limit overrides. Default False so
        # dynamic changes cannot silently alter the configured budget (issue #517).
        self.allow_batch_override = allow_batch_override
        # Auto-eviction on add-overflow (issue #1283): when a bare `add` would
        # exceed the char budget, evict the OLDEST entry and retry instead of
        # hard-rejecting. ``auto_evict_keep_min`` is a safety floor so one huge
        # entry can't wipe the whole store. See ``_evict_to_fit``.
        self.auto_evict_on_full = auto_evict_on_full
        self.auto_evict_keep_min = max(0, int(auto_evict_keep_min))
        # Frozen snapshot for system prompt -- set once at load_from_disk()
        self._system_prompt_snapshot: dict[str, str] = {"memory": "", "user": ""}
        # Optional memory-poisoning guard (issue #315). DEFAULT None: when unset,
        # the write path keeps its pre-#315 binary-block behaviour exactly (see
        # _gate_write). A MemoryGuardPolicy here routes a scan hit through
        # block/warn/strip actions keyed off provenance instead.
        self._guard = guard
        # Provenance of the write currently being gated; set by add/replace just
        # before calling _gate_write. Default None -> guard uses safe defaults.
        self._last_provenance = None
        # Per-turn counter of failed at-capacity consolidation attempts; reset
        # at each turn boundary by reset_consolidation_failures() (#42405).
        self._consolidation_failures = 0
        # Threat-blocked entries already warned about (issue #138). Loaded
        # lazily from a sidecar under the memory dir so a poisoned entry warns
        # ONCE across sessions instead of at every session start.
        self._blocked_warned: Optional[set] = None

    def target_enabled(self, target: str) -> bool:
        """Return whether this session's selected built-in store is writable."""
        return self.user_profile_enabled if target == "user" else self.memory_enabled

    def reset_consolidation_failures(self) -> None:
        """Reset the per-turn consolidation-failure counter (call at turn start)."""
        self._consolidation_failures = 0

    def _consolidation_failure(self, response: dict[str, Any]) -> dict[str, Any]:
        """Count a consolidation failure: under the per-turn cap return ``response``
        (it says how to retry); past it a TERMINAL result so the model stops looping.

        Under the per-turn cap, return ``response`` unchanged (it already tells
        the model how to self-correct + retry in this turn). Once the cap is
        exceeded, drop the retry instruction and return a TERMINAL result so the
        model stops looping memory calls and proceeds to answer the user — a
        failed memory side effect must never block the turn's reply (#42405).
        """
        self._consolidation_failures += 1
        if self._consolidation_failures <= self._MAX_CONSOLIDATION_FAILURES_PER_TURN:
            return response
        FAILURE_CLASS.set("retry_cap")
        return {
            "success": False,
            "done": True,
            "error": (
                f"Memory consolidation failed {self._consolidation_failures} times "
                "this turn. Stop retrying memory calls — leave memory unchanged for "
                "now and continue with your reply to the user. The fact can be saved "
                "in a later turn."
            ),
        }

    def _failure_with_entries(self, target: str, message: str) -> dict[str, Any]:
        """Consolidation failure carrying the live entries so the model can consolidate."""
        FAILURE_CLASS.set("over_budget")
        current = self._char_count(target)
        limit = self._char_limit(target)
        return self._consolidation_failure({
            "success": False,
            "error": message,
            "current_entries": self._entries_for(target),
            "current_size": current,
            "max_size": limit,
            "usage": f"{current:,}/{limit:,}",
        })

    def load_from_disk(self):
        """Load entries from MEMORY.md and USER.md, capture system prompt snapshot.

        The frozen snapshot is what enters the system prompt. We scan each
        entry for injection/promptware patterns at snapshot-build time —
        ANY hit replaces the entry text in the snapshot with a placeholder
        like ``[BLOCKED: …]``, so a poisoned-on-disk memory file (supply
        chain, compromised tool, sister-session write) cannot inject into
        the system prompt.

        The live ``memory_entries`` / ``user_entries`` lists keep the
        original text so the user can still SEE poisoned entries via
        see poisoned entries by inspecting the source files directly, and remove them — silently dropping them would hide the attack from the user.

        Scanning is deterministic from disk bytes, so the snapshot remains
        stable for the entire session (prefix-cache invariant holds).
        """
        mem_dir = get_memory_dir()
        mem_dir.mkdir(parents=True, exist_ok=True)

        self.memory_entries = self._read_file(mem_dir / "MEMORY.md")
        self.user_entries = self._read_file(mem_dir / "USER.md")

        # Deduplicate entries (preserves order, keeps first occurrence)
        self.memory_entries = list(dict.fromkeys(self.memory_entries))
        self.user_entries = list(dict.fromkeys(self.user_entries))

        # Sanitize entries for the system-prompt snapshot only.  Live state
        # (memory_entries / user_entries) keeps the raw text so the user
        # can see + remove poisoned entries via the memory tool.
        # Threat-blocked warning dedup (issue #138): entries whose text we
        # have already warned about are still replaced with the [BLOCKED:]
        # placeholder — only the WARNING is suppressed, so a poisoned entry
        # warns once per unique text instead of at every session start.
        warned = self._load_blocked_warnings()
        sanitized_memory = self._sanitize_entries_for_snapshot(
            self.memory_entries, "MEMORY.md", warned
        )
        sanitized_user = self._sanitize_entries_for_snapshot(
            self.user_entries, "USER.md", warned
        )
        if warned:
            self._persist_blocked_warnings(warned)

        # Capture frozen snapshot for system prompt injection
        self._system_prompt_snapshot = {
            "memory": self._render_block("memory", sanitized_memory),
            "user": self._render_block("user", sanitized_user),
        }
        # External writers (MCP bridges, hand edits) can exceed the cap; the limit only fires on
        # add/replace, so the oversized block would silently ride in the prompt while every later
        # add is refused with no visible cause (#10877). Warn; never truncate a user's memories.
        for target in ("memory", "user"):
            if (count := self._char_count(target)) > (limit := self._char_limit(target)):
                logger.warning("%s exceeds its char limit on load: %d/%d chars. Entries stay loaded; "
                               "further additions are blocked until it is back under the limit.",
                               self._path_for(target).name, count, limit)

    @staticmethod
    def _sanitize_entries_for_snapshot(
        entries: list[str],
        filename: str,
        warned: Optional[set] = None,
    ) -> list[str]:
        """Return ``entries`` with any threat-matching entry replaced by a placeholder.

        Each entry is scanned with the shared threat-pattern library at the
        ``"strict"`` scope (same as memory writes).  On match, the entry is
        replaced in the returned list with ``"[BLOCKED: <filename> entry
        contained threat pattern: <ids>. Removed from system prompt.]"`` —
        the placeholder enters the snapshot, the original entry stays in
        live state for the user to inspect and delete.

        Empty or already-block-marker entries pass through unchanged.

        Provenance and non-authorizing grounding status remain visible in the
        frozen snapshot. The scan still runs over the raw entry before rendering.
        Legacy files are not rewritten or silently promoted to user approval.
        """
        from tools.threat_patterns import scan_for_threats

        warned = warned if warned is not None else set()
        sanitized: list[str] = []
        for entry in entries:
            if not entry or entry.startswith("[BLOCKED:"):
                sanitized.append(entry)
                continue
            findings = scan_for_threats(entry, scope="strict")
            if findings:
                # Warn ONCE per unique entry text (issue #138): the raw entry
                # stays on disk by design (live state keeps it visible and
                # removable), so without dedup the same poisoned entry would
                # re-warn at every session start. The fingerprint changes when
                # the entry text changes, so an edited entry that still matches
                # re-warns — the suppression is per-text, not permanent.
                fp = f"{filename}|{hashlib.sha256(entry.encode('utf-8')).hexdigest()}"
                if fp not in warned:
                    logger.warning(
                        "Memory entry from %s blocked at load time: %s",
                        filename,
                        ", ".join(findings),
                    )
                    warned.add(fp)
                sanitized.append(
                    f"[BLOCKED: {filename} entry contained threat pattern(s): "
                    f"{', '.join(findings)}. Removed from system prompt; "
                    f"use memory(action=remove, target='{_store_target(filename)}') "
                    f"to delete the original.]"
                )
            else:
                text, source, trust, grounding = _entry_grounding(entry)
                sanitized.append(
                    f"[grounding={grounding}; source={source}; trust={trust}; authority=none]\n{text}"
                )
        return sanitized

    def _load_blocked_warnings(self) -> set:
        """Load the set of already-warned blocked-entry fingerprints.

        Best-effort: any read/parse failure degrades to an empty set — the
        only cost is one extra warning on the next load. Results are cached
        on the instance so repeated loads in one session read the file once.
        """
        if self._blocked_warned is not None:
            return self._blocked_warned
        warned: set = set()
        try:
            path = get_memory_dir() / BLOCKED_WARNINGS_FILE
            if path.exists():
                data = json.loads(path.read_text(encoding="utf-8-sig"))
                if isinstance(data, list):
                    warned = {str(item) for item in data if isinstance(item, str)}
        except (OSError, ValueError):
            logger.debug("Could not read blocked-warning dedup file", exc_info=True)
        self._blocked_warned = warned
        return warned

    def _persist_blocked_warnings(self, warned: set) -> None:
        """Persist the warned-fingerprint set (bounded, best-effort)."""
        try:
            items = sorted(warned)
            # Bounded: keep only the most recent BLOCKED_WARNINGS_MAX
            # fingerprints so a long-lived profile cannot grow the sidecar
            # without bound. Dropping an old fingerprint only ever costs one
            # re-warning for that exact text.
            if len(items) > BLOCKED_WARNINGS_MAX:
                items = items[-BLOCKED_WARNINGS_MAX:]
            get_memory_dir().mkdir(parents=True, exist_ok=True)
            atomic_write_text(
                get_memory_dir() / BLOCKED_WARNINGS_FILE,
                json.dumps(items, indent=2, sort_keys=True) + "\n",
                tmp_prefix=".mem_",
            )
        except (OSError, ValueError, RuntimeError):
            logger.debug("Could not persist blocked-warning dedup file", exc_info=True)

    # Max time to wait for a file lock before giving up with a clear error.
    _LOCK_TIMEOUT_SECONDS = 10.0

    @staticmethod
    @contextmanager
    def _file_lock(path: Path):
        """Acquire an exclusive file lock for read-modify-write safety.

        Uses a separate .lock file so the memory file itself can still be
        atomically replaced via os.replace().

        On Unix, uses non-blocking flock with bounded retry + exponential
        backoff so a stuck lock doesn't hang the agent indefinitely. If the
        lock can't be acquired within _LOCK_TIMEOUT_SECONDS, raises
        TimeoutError with a diagnostic message.
        """
        lock_path = path.with_suffix(path.suffix + ".lock")
        from tools import memory_tool as _mt  # fcntl/msvcrt live (and are patched) there
        fcntl, msvcrt = _mt.fcntl, _mt.msvcrt
        from hermes_constants import mkdir_under_hermes_home

        mkdir_under_hermes_home(lock_path.parent)
        if fcntl is None and msvcrt is None:
            yield
            return
        flags = os.O_RDWR | os.O_CREAT
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        raw_fd = os.open(lock_path, flags, 0o600)
        try:
            # The creation mode is filtered through the process umask and does
            # not repair a lock left loose by an older Hermes process. Tighten
            # the opened inode before acquiring the lock so both cases are
            # owner-only. Operating on the fd avoids a path-swap window.
            if hasattr(os, "fchmod"):
                os.fchmod(raw_fd, 0o600)
            fd = os.fdopen(raw_fd, "r+", encoding="utf-8")
        except Exception:
            os.close(raw_fd)
            raise
        with fd:
            try:
                if fcntl:
                    MemoryStore._acquire_fcntl_lock(fd)
                else:
                    fd.seek(0)
                    msvcrt.locking(fd.fileno(), msvcrt.LK_LOCK, 1)
                yield
            finally:
                with suppress(OSError, IOError):
                    if fcntl:
                        fcntl.flock(fd, fcntl.LOCK_UN)
                    elif msvcrt:
                        fd.seek(0)
                        msvcrt.locking(fd.fileno(), msvcrt.LK_UNLCK, 1)

    @staticmethod
    def _acquire_fcntl_lock(fd, timeout: Optional[float] = None) -> None:
        """Acquire an exclusive flock with bounded retry and backoff.

        Uses LOCK_EX | LOCK_NB (non-blocking) in a retry loop with
        exponential backoff (0.05s, 0.1s, 0.2s, ...). Raises TimeoutError
        if the lock isn't acquired within *timeout* seconds.
        """
        from tools import memory_tool as _mt  # tests patch fcntl here
        fcntl = _mt.fcntl
        timeout = timeout if timeout is not None else MemoryStore._LOCK_TIMEOUT_SECONDS
        deadline = time.monotonic() + timeout
        wait = 0.05
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return  # acquired
            except OSError:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(
                        f"Could not acquire memory file lock within "
                        f"{timeout:.0f}s — another process may hold it. "
                        f"Lock file: {fd.name}"
                    )
                sleep_time = min(wait, remaining)
                time.sleep(sleep_time)
                wait = min(wait * 2, 1.0)  # cap at 1s per attempt

    @staticmethod
    def _path_for(target: str) -> Path:
        mem_dir = get_memory_dir()
        if target == "user":
            return mem_dir / "USER.md"
        return mem_dir / "MEMORY.md"

    def _reload_target(self, target: str, *, skip_drift: bool = False):
        """Re-read entries from disk into in-memory state.

        Called under file lock to get the latest state before mutating.
        Returns the backup path if external drift was detected (the on-disk
        file contains content that wouldn't round-trip through our
        parser/serializer, OR an entry larger than the store's char limit).
        When drift is detected the caller must abort the mutation —
        flushing would discard the un-roundtrippable content.
        Returns ``None`` on clean reload.

        Returns the ``_READ_FAILED`` sentinel when the file EXISTS but could not
        be read. The caller MUST abort: the on-disk entries are unknown, so
        overwriting from an assumed-empty view would wipe them. This is the real
        exposure behind ``add`` — it skips the drift guard because appending is
        safe, but that reasoning only holds when the reload actually saw the
        file. A failed read reported as ``[]`` turned ``add`` into a full-file
        rewrite down to a single entry.

        When *skip_drift* is True the round-trip / entry-size check is
        bypassed.  Used by the ``add`` action which appends without
        rewriting, so existing content is never clobbered.
        """
        path = self._path_for(target)
        raw, read_ok = self._read_raw_checked(path)
        if not read_ok:
            # Leave in-memory entries untouched and tell the caller to abort;
            # persisting over an unreadable file would destroy it.
            return _READ_FAILED
        # Derive BOTH the drift check and the entry parse from the same raw
        # snapshot. The drift guard used to re-read the file itself and treat
        # a failed second read as "no drift" — so a read failure between the
        # checked reload and the drift check let replace/remove/apply_batch
        # rewrite the file from a stale view, silently discarding whatever an
        # external writer had just added. One read, one snapshot, no window.
        bak = None if skip_drift else self._detect_external_drift(target, raw)
        fresh = self._parse_entries(raw)
        fresh = list(dict.fromkeys(fresh))  # deduplicate
        self._set_entries(target, fresh)
        return bak

    def save_to_disk(self, target: str):
        """Persist entries to the appropriate file. Called after every mutation."""
        get_memory_dir().mkdir(parents=True, exist_ok=True)
        self._write_file(self._path_for(target), self._entries_for(target))

    def _entries_for(self, target: str) -> list[str]:
        return self.user_entries if target == "user" else self.memory_entries

    def _set_entries(self, target: str, entries: list[str]):
        setattr(self, "user_entries" if target == "user" else "memory_entries", entries)

    def _char_count(self, target: str) -> int:
        entries = self._entries_for(target)
        if not entries:
            return 0
        return len(ENTRY_DELIMITER.join(entries))

    def _usage(self, target: str) -> str:
        """``current/limit`` for recoverable tool errors (approval-time inventory)."""
        limit = self._char_limit(target)
        return f"{self._char_count(target):,}/{limit:,}"

    def _evict_to_fit(
        self,
        target: str,
        new_entry: str,
        limit: int,
    ) -> Optional[list[str]]:
        """Evict OLDEST entries until ``new_entry`` fits ``limit`` (issue #1283).

        Returns the list of evicted DISPLAY texts (oldest-first), or ``None``
        when the add still doesn't fit — e.g. the single new entry alone
        exceeds the limit, or the safety floor (``auto_evict_keep_min``) was
        reached before it fit. The caller must treat ``None`` as "give up and
        return the consolidation-failure response" so behaviour degrades
        gracefully to the pre-#1283 path.

        Entries are stored in insertion order (a §-delimited file is
        append-mostly), so index 0 is the oldest — that is what we evict.
        We never evict below ``auto_evict_keep_min`` entries. Only the
        ``memory`` target is eligible; ``user`` profile facts are scarce and
        high-value, so they keep the manual-consolidation path.
        """
        if not self.auto_evict_on_full or target != "memory":
            return None

        entries = self._entries_for(target)
        # Working copy we mutate locally; only commit back if it fits.
        working = list(entries)
        evicted: list[str] = []

        while True:
            candidate = working + [new_entry]
            if len(ENTRY_DELIMITER.join(candidate)) <= limit:
                return evicted
            # Can't evict further without breaching the safety floor.
            if len(working) <= self.auto_evict_keep_min:
                return None
            evicted.append(parse_provenance(working.pop(0))[0])

    def _batch_failure(self, target: str, message: str, failure_class: str,
                       closest_entries: Optional[list[str]] = None) -> dict[str, Any]:
        """Batch-abort failure WITHOUT ``current_entries``: the store did not change and the
        caller already holds the inventory, so echoing it made each consolidation retry
        grow the context it was invoked to shrink (#97316). A zero-match op carries only
        its few ``closest_entries``."""
        return self._consolidation_failure(
            _error(message + " No operations were applied (batch is all-or-nothing).", failure_class,
                   usage=self._usage(target),
                   **({"closest_entries": closest_entries} if closest_entries else {})))

    def _mutate(self, target: str, mutate, *, skip_drift: bool = False) -> dict[str, Any]:
        """Lock, re-read from disk, run ``mutate(entries, limit)`` -> ``(new_entries, message)``
        or an error dict, then persist and return the success response. The reload aborts
        on an existing-but-unreadable file (even append-only ``add`` rewrites the whole
        file) and, unless *skip_drift*, on external drift (flushing would discard
        un-roundtrippable content). Drift check and parse use the SAME raw snapshot —
        a failed second read used to count as "no drift". The closure may return a
        third value, a dict merged into the success payload (``_error``'s ``**extra``
        convention) — e.g. the full text a replace overwrote (#117952). ANY dict the closure
        returns is passed through verbatim and nothing is persisted: error dicts, or the
        success payload of a read-only closure (``resolve_entry``, ``resolve_batch_entries``)."""
        path = self._path_for(target)
        with self._file_lock(path):
            raw, read_ok = self._read_raw_checked(path)
            if not read_ok:
                return _read_failed_error(path)
            bak = None if skip_drift else self._detect_external_drift(target, raw)
            self._set_entries(target, list(dict.fromkeys(self._parse_entries(raw))))
            if bak:
                return _drift_error(path, bak)
            result = mutate(self._entries_for(target), self._char_limit(target))
            if isinstance(result, dict):
                return result
            self._set_entries(target, result[0])
            from hermes_constants import mkdir_under_hermes_home

            mkdir_under_hermes_home(path.parent)
            self._write_file(path, result[0])
            extra_fields = result[2] if len(result) > 2 else {}
            return self._success_response(target, result[1], **extra_fields)

    def _evict_replacement_to_fit(
        self,
        target: str,
        entries: list[str],
        new_text: str,
        limit: int,
    ) -> Optional[tuple[list[str], list[str]]]:
        """Evict OLDEST entries (never ``new_text``) until the list fits ``limit``.

        Issue #129 — the replace-path twin of ``_evict_to_fit`` (#1283): the
        add path auto-evicts on overflow but replace still hard-rejected, so
        nightly consolidation (dreaming) writes at cap died with
        'Replacement would put memory at N/M chars'. The caller passes the
        working list ALREADY containing the replacement at its target index;
        this evicts the oldest OTHER entries (matched by stored text so the
        replacement itself is never a victim) until the list fits.

        Returns ``(evicted_display_texts, surviving_entries)`` — oldest-first
        texts plus the exact surviving list — or ``None`` when it still
        doesn't fit (the replacement alone exceeds the limit, or the safety
        floor ``auto_evict_keep_min`` was reached). Callers must treat
        ``None`` as "give up and return the consolidation-failure response",
        exactly like the add path. Only the ``memory`` target is eligible;
        ``user`` facts are scarce/high-value and keep the manual path.
        """
        if not self.auto_evict_on_full or target != "memory":
            return None

        working = list(entries)
        evicted: list[str] = []

        while True:
            if len(ENTRY_DELIMITER.join(working)) <= limit:
                return evicted, working
            # Can't evict further without breaching the safety floor.
            if len(working) <= self.auto_evict_keep_min:
                return None
            # Oldest entry that is not the replacement itself.
            victim = next(
                (i for i, e in enumerate(working) if e != new_text),
                None,
            )
            if victim is None:
                return None
            evicted.append(parse_provenance(working.pop(victim))[0])

    def _char_limit(self, target: str, dynamic_limit: Optional[int] = None) -> int:
        """Return the effective char limit for ``target``.

        Per-issue #517, a caller may pass a one-off ``dynamic_limit`` to
        ``apply_batch``. It is only honoured for the ``memory`` target when
        ``self.allow_batch_override`` is True; the ``user`` target always uses
        its configured limit. The system-prompt snapshot always uses the
        configured limits, so a dynamic batch override cannot invalidate the
        prefix cache.
        """
        if target == "user":
            return self.user_char_limit
        if dynamic_limit is None or not self.allow_batch_override:
            return self.memory_char_limit
        return int(dynamic_limit)

    def _gate_write(self, content: str):
        """Decide whether ``content`` may be written, reusing the threat scanner.

        Returns ``(error, effective_content, guard_event)``:

        * ``error`` — a non-None error string means BLOCK the write.
        * ``effective_content`` — the content to actually store (may differ from
          the input only when a guard ``strip`` action fired).
        * ``guard_event`` — an optional structured dict describing a warn/strip
          decision, for the caller to log; ``None`` for the legacy path and for
          clean content.

        DEFAULT-OFF / BACKWARD-COMPAT (issue #315): when ``self._guard`` is
        ``None`` (the default) this collapses to the pre-#315 behaviour — the
        existing binary ``_scan_memory_content`` block — so clean writes pass and
        poisoned writes are refused exactly as before, with no strip/warn and no
        event. The guard only participates when explicitly configured on.
        """
        if self._guard is None:
            # Legacy path: binary block, byte-identical to pre-#315.
            return _scan_memory_content(content), content, None

        # The entry's provenance is already resolved by the caller into
        # self._last_provenance; the guard routes its action off the source
        # class. It reuses the existing scanner internally for detection.
        outcome = self._guard.evaluate(content, self._last_provenance)
        if not outcome.allowed:
            return outcome.message, content, None
        if outcome.action in ("warn", "strip"):
            return None, outcome.content, outcome.to_event()
        # allow (clean content, or an explicit allow rule): no event.
        return None, outcome.content, None

    def add(
        self,
        target: str,
        content: str,
        source_class: str = DEFAULT_SOURCE_CLASS,
        trust_tier: str = DEFAULT_TRUST_TIER,
    ) -> dict[str, Any]:
        """Append a new entry. Returns error if it would exceed the char limit.

        ``source_class`` / ``trust_tier`` tag the entry's provenance (#316).
        When both are the safe defaults the entry is stored verbatim (no
        trailer) so the on-disk format is byte-identical to pre-#316.
        """
        content = content.strip()
        if not content:
            FAILURE_CLASS.set("missing_content")
            return {"success": False, "error": "Content cannot be empty."}

        prov_error = _validate_provenance(source_class, trust_tier)
        if prov_error:
            FAILURE_CLASS.set("invalid_args")
            return {"success": False, "error": prov_error}

        # Scan the user-visible content (not the provenance trailer) for
        # injection/exfiltration before accepting. With no guard configured
        # (default) this is the pre-#315 binary block; a configured guard may
        # instead warn (store as-is) or strip (store the excised content).
        if self._guard is not None:
            self._last_provenance = _make_provenance(source_class, trust_tier)
        scan_error, content, guard_event = self._gate_write(content)
        if scan_error:
            FAILURE_CLASS.set("scan_blocked")
            return {"success": False, "error": scan_error}
        if guard_event is not None:
            _log_guard_event("add", target, guard_event)

        # The string actually stored on disk carries the optional trailer.
        stored = encode_provenance(content, source_class, trust_tier)

        with self._file_lock(self._path_for(target)):
            # Re-read from disk under lock to pick up writes from other sessions.
            # For add (append-only), we skip the drift guard — appending never
            # clobbers existing content, so round-trip mismatches from prior
            # tool-written entries in the same session are harmless.  The drift
            # guard remains active for replace/remove where full-file rewrite
            # would discard un-roundtrippable content (issue #26045).
            #
            # But "append never clobbers" only holds when the reload actually
            # read the file. add rewrites the WHOLE file from the parsed
            # entries, so a file that exists but read as empty (transient lock,
            # permission blip, I/O error) would be rewritten down to just the
            # new entry — wiping every prior memory. Refuse instead.
            if self._reload_target(target, skip_drift=True) is _READ_FAILED:
                return _read_failed_error(self._path_for(target))

            entries = self._entries_for(target)
            limit = self._char_limit(target)

            # Reject exact duplicates (compare on the stored form, which
            # includes provenance — a re-tag of the same text is not a dup).
            if stored in entries:
                return self._success_response(
                    target, "Entry already exists (no duplicate added)."
                )

            # Calculate what the new total would be
            new_entries = entries + [stored]
            new_total = len(ENTRY_DELIMITER.join(new_entries))

            if new_total > limit:
                # Auto-eviction (issue #1283): try to make room by evicting
                # the OLDEST entries instead of hard-rejecting and forcing a
                # multi-call read/evict/rewrite spiral. ``_evict_to_fit``
                # returns the evicted display texts (oldest-first) on success
                # or None when the add still can't fit (giant entry, or the
                # safety floor was reached) — in which case we fall through to
                # the existing consolidation-failure response below.
                evicted = self._evict_to_fit(target, stored, limit)
                if evicted is not None:
                    working = self._entries_for(target)
                    # Drop as many oldest entries as were evicted, then append.
                    del working[: len(evicted)]
                    working.append(stored)
                    self._set_entries(target, working)
                    self.save_to_disk(target)
                    logger.info(
                        "memory auto-evicted %d oldest entr%s to fit a new add "
                        "(target=%s)",
                        len(evicted),
                        "y" if len(evicted) == 1 else "ies",
                        target,
                    )
                    resp = self._success_response(
                        target,
                        f"Entry added; auto-evicted {len(evicted)} oldest "
                        f"entr{'y' if len(evicted) == 1 else 'ies'} to make room.",
                    )
                    # Surface exactly what was dropped — never silent.
                    resp["evicted"] = evicted
                    return resp

                FAILURE_CLASS.set("over_budget")
                current = self._char_count(target)
                return self._consolidation_failure({
                    "success": False,
                    "error": (
                        f"Memory at {current:,}/{limit:,} chars. "
                        f"Adding this entry ({len(content)} chars) would exceed the limit by {new_total - limit:,} chars; free at least {new_total - limit:,} chars. "
                        f"Consolidate now: use 'patch' to shorten overlapping entries into "
                        f"shorter ones or 'remove' stale or less important entries (see "
                        f"current_entries below), then retry this add — all in this turn."
                    ),
                    "current_entries": entries,
                    "current_size": current,
                    "max_size": limit,
                    "would_be_size": new_total,
                    "usage": f"{current:,}/{limit:,}",
                })

            entries.append(stored)
            self._set_entries(target, entries)
            self.save_to_disk(target)

        return self._success_response(target, "Entry added.")

    def replace(
        self,
        target: str,
        old_text: str,
        new_content: str,
        source_class: str = DEFAULT_SOURCE_CLASS,
        trust_tier: str = DEFAULT_TRUST_TIER,
        matched_entry: Optional[str] = None,
    ) -> dict[str, Any]:
        """Find entry containing old_text substring, replace it with new_content.

        ``source_class`` / ``trust_tier`` re-tag the replacement's provenance
        (#316). Defaults keep the stored form trailer-free (byte-compatible).
        The ``old_text`` match runs against each entry's DISPLAY text so a user
        matching on visible content still finds an entry that carries a trailer.
        """
        old_text = old_text.strip()
        new_content = new_content.strip()
        if not old_text:
            return {"success": False, "error": "old_text cannot be empty."}
        if not new_content:
            return {
                "success": False,
                "error": "new_content cannot be empty. Use 'remove' to delete entries.",
            }

        prov_error = _validate_provenance(source_class, trust_tier)
        if prov_error:
            return {"success": False, "error": prov_error}

        # Scan replacement content for injection/exfiltration. Guard-off
        # (default) = pre-#315 binary block; guard-on may warn or strip.
        if self._guard is not None:
            self._last_provenance = _make_provenance(source_class, trust_tier)
        scan_error, new_content, guard_event = self._gate_write(new_content)
        if scan_error:
            return {"success": False, "error": scan_error}
        if guard_event is not None:
            _log_guard_event("replace", target, guard_event)

        stored_new = encode_provenance(new_content, source_class, trust_tier)

        with self._file_lock(self._path_for(target)):
            bak = self._reload_target(target)
            if bak is _READ_FAILED:
                return _read_failed_error(self._path_for(target))
            if bak:
                return _drift_error(self._path_for(target), bak)

            entries = self._entries_for(target)
            idx, loc_err = _locate_stored(entries, old_text, matched_entry)
            if loc_err:
                return loc_err
            if idx is None:
                return self._consolidation_failure({
                    "success": False,
                    "error": f"No entry matched '{old_text}'. Check current_entries below and retry with the exact text of the entry you want to replace.",
                    "current_entries": entries,
                })
            # old_text only locates the entry. content is the COMPLETE new
            # entry (#117952) — surface the full text that was overwritten so
            # a whole-entry write is never silent.
            overwritten = entries[idx]
            limit = self._char_limit(target)

            # Check that replacement doesn't blow the budget
            test_entries = entries.copy()
            test_entries[idx] = stored_new
            new_total = len(ENTRY_DELIMITER.join(test_entries))

            if new_total > limit:
                # Issue #129 — mirror the add-path auto-eviction (#1283) for
                # replace-overflow: evict the OLDEST entries (never the entry
                # being replaced) until the replaced set fits, instead of
                # hard-rejecting the nightly consolidation (dreaming) writes
                # that run at cap. ``None`` degrades to the pre-#129 response.
                outcome = self._evict_replacement_to_fit(
                    target, test_entries, stored_new, limit
                )
                if outcome is not None:
                    evicted, surviving = outcome
                    self._set_entries(target, surviving)
                    self.save_to_disk(target)
                    logger.info(
                        "memory replace overflow: evicted %d old entr%s to fit",
                        len(evicted),
                        "y" if len(evicted) == 1 else "ies",
                    )
                    return self._success_response(
                        target,
                        f"Entry replaced (evicted {len(evicted):d} oldest entr{'y' if len(evicted) == 1 else 'ies'} to make room).",
                        replaced_entry=overwritten,
                    )
                current = self._char_count(target)
                return self._consolidation_failure({
                    "success": False,
                    "error": (
                        f"Replacement would put memory at {new_total:,}/{limit:,} chars. "
                        f"Shorten the new content, or 'remove' other stale or less important "
                        f"entries to make room (see current_entries below), then retry — all "
                        f"in this turn."
                    ),
                    "current_entries": entries,
                    "current_size": current,
                    "max_size": limit,
                    "would_be_size": new_total,
                    "usage": f"{current:,}/{limit:,}",
                })

            entries[idx] = stored_new
            self._set_entries(target, entries)
            self.save_to_disk(target)

        return self._success_response(
            target, "Entry replaced.", replaced_entry=overwritten
        )

    def _locate(self, entries: list[str], old_text: str, verb: str, matched_entry: Optional[str] = None):
        """Index of the entry *old_text* selects, or the error dict the edit returns. A write
        staged for approval carries the FULL entry it was reviewed against (*matched_entry*):
        only that exact entry qualifies, so replay never hits a newer entry that still
        contains old_text."""
        if matched_entry is not None:
            idx = _pinned_index(entries, matched_entry)
            return idx if idx is not None else _error(_stale_entry_message(matched_entry), "stale_entry")
        idx, ambiguous = _find_unique_match(entries, old_text)
        if ambiguous:
            return _error(f"Multiple entries matched '{old_text}'. Be more specific.", "ambiguous",
                          matches=[entries[i][:80] + ("..." if len(entries[i]) > 80 else "")
                                   for i in _substring_matches(entries, old_text)])
        if idx is None:
            return self._consolidation_failure(_no_match_error(entries, old_text, verb, current_entries=entries))
        return idx


    @staticmethod
    def _apply_batch_op(working: list[str], act: str, content: str, old_text: str, pos: str,
                        matched_entry: Optional[str] = None) -> tuple[Optional[str], Any, str, Optional[str]]:
        """Apply one batch op to *working*; return ``(error message, previous content, failure class,
        patched entry)`` — on a zero-match error the second value is that op's ``closest_entries``
        instead, and the fourth is the entry a patch produced (None for every other op).
        Previous content is captured before each replace/remove, under the store lock.
        It is published only after the entire batch has been validated and persisted.
        A staged op's *matched_entry* selects exactly that entry, as in ``_locate``.
        """
        if act == "add":
            if not content:
                return f"{pos}: content is required.", None, "missing_content", None
            if content not in working:  # idempotent -- skip duplicate, don't fail the batch
                working.append(content)
            return None, None, "none", None
        if act not in ("replace", "remove", "patch"):
            return f"{pos}: unknown action. Use add, patch, or remove.", None, "invalid_args", None
        if not old_text.strip():
            return f"{pos}: old_text is required.", None, "missing_old_text", None
        if act == "replace" and not content:
            return (f"{pos}: content is required — set this operation object's own 'content' to the COMPLETE "
                    f"new entry (or use action='remove' to delete)."), None, "missing_content", None
        if act == "patch" and not content.strip():
            return (f"{pos}: content is required — set this operation object's own 'content' to the new "
                    f"text for the old_text span. {PATCH_DROP_HINT}"), None, "missing_content", None
        if matched_entry is not None:
            idx = _pinned_index(working, matched_entry)
            if idx is None:
                return f"{pos}: {_stale_entry_message(matched_entry)}", None, "stale_entry", None
        else:
            idx, ambiguous = _find_unique_match(working, old_text)
            if ambiguous:
                return (f"{pos}: '{old_text}' matched multiple distinct entries -- be more specific.", None,
                        "ambiguous", None)
            if idx is None:
                miss = _no_match_error(working, old_text, act)
                return f"{pos}: {miss['error']}", miss.get("closest_entries"), "no_match", None
        previous_content = working[idx]
        if act != "patch":
            working[idx:idx + 1] = [content] if act == "replace" else []
            return None, previous_content, "none", None
        patched, message, failure_class = _patch_entry(previous_content, old_text, content)
        if patched is None:
            return f"{pos}: {message}", None, failure_class, None
        working[idx:idx + 1] = [patched]
        return None, previous_content, "none", patched


    def _batch(self, target: str, operations: list[dict[str, Any]], *, commit: bool, memory_char_limit: Optional[int] = None) -> dict[str, Any]:
        from tools.memory_tool import _memory_target_error
        if target_error := _memory_target_error(self, target, operations):
            return target_error
        if not operations:
            return _error("operations list is empty.", "invalid_args")
        ops = [op or {} for op in operations]
        # Scan every add/replace content BEFORE touching disk -- one poisoned op rejects the batch.
        for i, op in enumerate(ops):
            scan_error = op.get("action") in {"add", "replace"} and op.get("content") and _scan_memory_content(op["content"])
            if scan_error:
                return _error(f"Operation {i + 1}: {scan_error}", "scan_blocked")

        def _apply(entries, limit):
            limit = self._char_limit(target, dynamic_limit=memory_char_limit)
            working = list(entries)  # only committed if the whole batch validates
            matched = []  # per op, the entry a replace/patch/remove selected (None for add)
            produced = {}  # 1-based op index -> the entry a patch produced
            for i, op in enumerate(ops):
                act = op.get("action")
                content, old_text = op.get("content") or op.get("new_text") or "", op.get("old_text") or ""
                if act != "patch":  # whole-entry actions; a patch span keeps its whitespace
                    content, old_text = content.strip(), old_text.strip()
                msg, previous_content, failure_class, after = self._apply_batch_op(
                    working, act, content, old_text, f"Operation {i + 1} ({act or 'unknown'})",
                    op.get("matched_entry"))
                if msg:  # a failed op's second value is its closest_entries (or None)
                    return self._batch_failure(target, msg, failure_class, closest_entries=previous_content)
                matched.append(previous_content)
                if after is not None:
                    produced[i + 1] = after
            if entries and not working:
                # #103419: a consolidation batch that removes the last entry would
                # commit an empty file as a normal successful write. Refuse; single
                # remove() is the deliberate-wipe path.
                label = self._path_for(target).name
                return self._batch_failure(target, (
                    f"Refusing to empty {label}: this batch would remove every entry from a "
                    f"previously non-empty store. Keep at least one entry — merge overlapping "
                    f"entries into a shorter one instead of removing the last one. To delete the "
                    f"final entry deliberately, use single remove() calls."), "would_empty")
            new_total = len(ENTRY_DELIMITER.join(working))  # budget check against the FINAL state only
            if new_total > limit:
                failed = self._batch_failure(target, (
                    f"Applying all {len(operations)} operations {_over_limit(new_total, limit)} more. Add "
                    f"removes or shortening patches of other stale entries to the same batch, then retry."),
                    "over_budget")
                if not failed.get("done"):
                    failed.update(current_size=self._char_count(target), max_size=limit, would_be_size=new_total)
                return failed
            if not commit:
                return {"success": True, "matched_entries": matched}
            # op index -> full entry text its replace/remove selected (#117952), 1-based to
            # match the "Operation N" error numbering the model sees for failed ops.
            replaced, removed, patched = {}, {}, {}
            for i, (op, previous_content) in enumerate(zip(ops, matched), 1):
                if previous_content is not None:
                    result_map = {"replace": replaced, "patch": patched, "remove": removed}.get(op.get("action"))
                    if result_map is not None:
                        result_map[i] = previous_content
            replaced_fields = {"replaced_entries": replaced} if replaced else {}
            if patched:
                replaced_fields["patched_entries"] = patched
                replaced_fields["patched_entries_after"] = produced
            if removed:
                replaced_fields["removed_entries"] = removed
            replaced_fields["limit"] = limit
            return working, f"Applied {len(operations)} operation(s).", replaced_fields
        return self._mutate(target, _apply, skip_drift=not commit)


    def _usage_pct(self, target: str, current: int) -> str:
        limit = self._char_limit(target)
        return f"{min(100, int((current / limit) * 100)) if limit > 0 else 0}% — {current:,}/{limit:,} chars"


    def patch(self, target: str, old_text: str, new_text: str,
              matched_entry: Optional[str] = None) -> dict[str, Any]:
        """Find the entry containing *old_text* and replace that one span with *new_text*;
        the rest of the entry is preserved verbatim. This is the partial-edit shape
        ``replace`` deliberately is not (replace overwrites the WHOLE entry). A span that
        occurs more than once in the entry is rejected rather than edited everywhere."""
        if not (old_text or "").strip():
            return _error("old_text cannot be empty.", "missing_old_text")
        if new_text is None or not str(new_text).strip():
            return _error(f"new_text cannot be empty. {PATCH_DROP_HINT}", "missing_content")
        # The span keeps its whitespace: it is part of the text being replaced ("tea " -> "coffee ").
        return self._patch(target, old_text, str(new_text), matched_entry)

    def _patch(self, target: str, old_text: str, new_text: str,
               matched_entry: Optional[str] = None) -> dict[str, Any]:
        """Locked find-and-splice inside the matched entry (#124582)."""
        def _apply(entries, limit):
            idx = self._locate(entries, old_text, "patch", matched_entry)
            if isinstance(idx, dict):
                return idx
            before = entries[idx]
            after, message, failure_class = _patch_entry(before, old_text, new_text)
            if after is None:
                return _error(message, failure_class)
            patched = entries[:idx] + [after] + entries[idx + 1:]
            new_total = len(ENTRY_DELIMITER.join(patched))
            if new_total > limit:
                return self._failure_with_entries(target, (
                    f"Patch would put memory at {new_total:,}/{limit:,} chars. Shorten the new "
                    f"text, or 'remove' other stale or less important entries to make room "
                    f"(see current_entries below), then retry — all in this turn."))
            return patched, "Entry patched.", {
                "patched_entry_before": before, "patched_entry_after": after}
        return self._mutate(target, _apply)

    def remove(self, target: str, old_text: str, matched_entry: Optional[str] = None) -> dict[str, Any]:
        """Remove the entry containing old_text substring."""
        old_text = old_text.strip()
        if not old_text:
            FAILURE_CLASS.set("missing_old_text")
            return {"success": False, "error": "old_text cannot be empty."}

        with self._file_lock(self._path_for(target)):
            bak = self._reload_target(target)
            if bak is _READ_FAILED:
                return _read_failed_error(self._path_for(target))
            if bak:
                return _drift_error(self._path_for(target), bak)

            entries = self._entries_for(target)
            idx, loc_err = _locate_stored(entries, old_text, matched_entry)
            if loc_err:
                return loc_err
            if idx is None:
                FAILURE_CLASS.set("no_match")
                return self._consolidation_failure({
                    "success": False,
                    "error": f"No entry matched '{old_text}'. Check current_entries below and retry with the exact text of the entry you want to remove.",
                    "current_entries": entries,
                })
            removed_entry = entries.pop(idx)
            self._set_entries(target, entries)
            self.save_to_disk(target)

        return self._success_response(
            target, "Entry removed.", removed_entry=removed_entry
        )

    def compact(
        self,
        target: str,
        target_size: int | None = None,
        prefer: str = "longest",
    ) -> dict[str, Any]:
        """Shorten entries until the store fits ``target_size`` or no more can be trimmed.

        This is the explicit compact/shorten helper requested in #516. It is a
        destructive operation in the sense that entry text is shortened, but it
        preserves the *semantic ordering* of entries and never drops an entry
        entirely. The agent can call it before a write that would otherwise fail.

        * ``target_size`` — goal in characters. Defaults to ``_char_limit`` so
          the result is guaranteed to fit.
        * ``prefer`` — which entries to trim first. ``longest`` (default) trims
          the longest entries first because they yield the biggest reductions.
          ``oldest`` trims the earliest entries first; in a §-delimited file
          that is insertion order, so it matches "oldest first".

        Trimming strategy: remove trailing sentences/words, keeping the first
        sentence/phrase intact. We never truncate mid-word in a way that
        leaves the leading entry meaningless.

        Returns a structured result including ``bytes_saved``, ``entries_changed``,
        and the usual ``usage``/``current_size``/``max_size`` fields.
        """
        if target not in {"memory", "user"}:
            return {
                "success": False,
                "error": f"Invalid target '{target}'. Use 'memory' or 'user'.",
            }
        if prefer not in {"longest", "oldest"}:
            return {"success": False, "error": "prefer must be 'longest' or 'oldest'."}

        limit = self._char_limit(target)
        goal = min(target_size if target_size is not None else limit, limit)

        with self._file_lock(self._path_for(target)):
            bak = self._reload_target(target)
            if bak:
                return _drift_error(self._path_for(target), bak)

            entries = self._entries_for(target)
            start_total = self._char_count(target)
            if start_total <= goal:
                resp = self._success_response(
                    target,
                    message=f"Memory already fits ({start_total:,} chars ≤ {goal:,}). No compaction needed.",
                )
                resp["bytes_saved"] = 0
                resp["entries_changed"] = 0
                return resp

            # Resolve display text (strip provenance trailers) for trimming; we
            # re-encode provenance on the shortened entry so tags are preserved.
            parsed = [parse_provenance(e) for e in entries]

            if prefer == "longest":
                order = sorted(
                    range(len(entries)), key=lambda i: len(parsed[i][0]), reverse=True
                )
            else:
                order = list(range(len(entries)))

            working_text = [text for text, _, _ in parsed]
            working_src = [src for _, src, _ in parsed]
            working_tier = [tier for _, _, tier in parsed]

            overage = start_total - goal
            changed_indices: set = set()
            for idx in order:
                if overage <= 0:
                    break
                text = working_text[idx]
                if not text:
                    continue
                # Trim from the end. A half-length floor left a single 300-char
                # entry at 150 when the caller asked for 50 — the result is
                # supposed to fit. _shorten_text still stops on a word boundary.
                min_keep = 1
                room_to_trim = len(text) - min_keep
                if room_to_trim <= 0:
                    continue
                trim = min(room_to_trim, overage + 1)
                trimmed = self._shorten_text(text, trim)
                if trimmed != text:
                    working_text[idx] = trimmed
                    changed_indices.add(idx)
                    overage -= len(text) - len(trimmed)

            new_entries = [
                encode_provenance(working_text[i], working_src[i], working_tier[i])
                for i in range(len(entries))
            ]
            new_total = len(ENTRY_DELIMITER.join(new_entries)) if new_entries else 0
            bytes_saved = start_total - new_total
            self._set_entries(target, new_entries)
            self.save_to_disk(target)

        resp = self._success_response(
            target, message=f"Compacted {len(changed_indices)} entr(y/ies)."
        )
        resp["bytes_saved"] = bytes_saved
        resp["entries_changed"] = len(changed_indices)
        resp["target_size"] = goal
        resp["current_size"] = new_total
        resp["max_size"] = limit
        resp["usage"] = (
            f"{min(100, int((new_total / limit) * 100)) if limit else 0}% — {new_total:,}/{limit:,} chars"
        )
        return resp

    @staticmethod
    def _shorten_text(text: str, trim_chars: int) -> str:
        """Remove up to ``trim_chars`` from the end of ``text`` at word/sentence boundaries.

        Tries, in order: sentence boundary, clause boundary (comma/semicolon),
        word boundary, then hard character truncation. Always returns a
        non-empty string with the leading portion preserved.
        """
        # Work on the raw text; provenance is handled by the caller.
        target_len = max(1, len(text) - trim_chars)
        if target_len >= len(text):
            return text

        # 1. Sentence boundary before target length.
        for i in range(target_len, len(text)):
            if text[i] in ".!?":
                candidate = text[: i + 1].rstrip()
                if (
                    len(candidate) <= len(text) - trim_chars
                    or len(candidate) <= target_len
                ):
                    return candidate
        # 2. Clause boundary.
        for i in range(target_len, len(text)):
            if text[i] in ",;:":
                candidate = text[:i].rstrip()
                if candidate and (
                    len(candidate) <= len(text) - trim_chars
                    or len(candidate) <= target_len
                ):
                    return candidate
        # 3. Word boundary.
        for i in range(target_len, -1, -1):
            if text[i].isspace():
                candidate = text[:i].rstrip()
                if candidate:
                    return candidate
        # 4. Hard truncate (preserve at least one char).
        return text[: max(1, target_len)].rstrip()

    def search(
        self,
        target: str,
        source_filter: Optional[object] = None,
        min_trust: Optional[str] = None,
        include_superseded: bool = False,
    ) -> list[dict[str, Any]]:
        """Return live entries as provenance-resolved rows, optionally filtered.

        Each row is ``{"text": <display>, "source_class": ..., "trust_tier": ...}``.
        Entries with no provenance trailer (legacy + default adds) resolve to
        the safe defaults. With NO filters this returns every entry in order —
        the no-filter call is the byte-compatible "read everything" path.

        Filters (#316 retrieval-time selection — tagging only, no enforcement):
          * ``source_filter``: a source_class string or iterable of them; keep
            entries whose source_class is in the set.
          * ``min_trust``: a trust tier; keep entries whose tier ranks >= it.
          * ``include_superseded`` (#2437): when False (default), superseded
            entries are hidden from recall. When True, every superseded row
            carries an extra ``superseded_at`` timestamp field (``None`` for
            live entries).
        """
        if isinstance(source_filter, str):
            allowed = {source_filter}
        elif source_filter is None:
            allowed = None
        else:
            allowed = set(source_filter)

        min_rank = _trust_rank(min_trust) if min_trust is not None else None

        rows: list[dict[str, Any]] = []
        for entry in self._entries_for(target):
            # Supersession marker is OUTERMOST, so parse it before provenance
            # (parse_supersession returns the display text with any provenance
            # trailer still attached).
            entry_display, sup_ts = parse_supersession(entry)
            if sup_ts is not None and not include_superseded:
                continue
            text, src, tier = parse_provenance(entry_display)
            if allowed is not None and src not in allowed:
                continue
            if min_rank is not None and _trust_rank(tier) < min_rank:
                continue
            row = {"text": text, "source_class": src, "trust_tier": tier,
                   "grounding": _entry_grounding(entry)[3], "authorizes_actions": False}
            if sup_ts is not None:
                row["superseded_at"] = sup_ts
            rows.append(row)
        return rows

    def supersede(self, target: str, old_text: str) -> dict[str, Any]:
        """Mark the entry containing *old_text* as superseded (#2437).

        Temporal governance: bytes stay on disk (audit trail) but recall
        hides the entry unless ``include_superseded=True``. First
        supersession wins — if the matched entry is already superseded, the
        original timestamp is preserved and the operation is a no-op.
        Runs under the file lock like replace/remove.
        """
        old_text = (old_text or "").strip()
        if not old_text:
            return {"success": False, "error": "old_text cannot be empty."}
        with self._file_lock(self._path_for(target)):
            bak = self._reload_target(target)
            if bak is _READ_FAILED:
                return _read_failed_error(self._path_for(target))
            if bak:
                return _drift_error(self._path_for(target), bak)
            entries = self._entries_for(target)
            result = supersede_entry(entries, old_text)
            if not result.get("success"):
                return self._consolidation_failure({
                    "success": False,
                    "error": result.get("error", "supersede failed."),
                    "current_entries": entries,
                })
            self.save_to_disk(target)
            msg = (
                "Entry already superseded (no change)."
                if result.get("already_superseded")
                else "Entry superseded; hidden from default recall."
            )
            return self._success_response(target, msg)

    def apply_batch(self, target: str, operations: list[dict[str, Any]], memory_char_limit: Optional[int] = None) -> dict[str, Any]:
        """Apply add/replace/remove ops atomically against the FINAL budget, so one call
        can free space and add entries. All-or-nothing: any malformed / unmatched op or
        an over-limit result writes NOTHING and returns the first failure. Aborts do not
        echo ``current_entries`` — the store is unchanged and the model already has it."""
        return self._batch(target, operations, commit=True, memory_char_limit=memory_char_limit)

    def _batch_error(
        self, target: str, message: str, limit: Optional[int] = None
    ) -> dict[str, Any]:
        """Batch-abort error without ``current_entries``.

        The store did not change. Echoing it made each consolidation retry pay
        the whole file back and grow the context the batch was meant to shrink
        (#97316). Single-op misses still return ``current_entries`` — the model
        has no other inventory on that path. Once the per-turn cap is hit,
        ``_consolidation_failure`` drops every field except the terminal error.
        """
        current = self._char_count(target)
        effective_limit = limit if limit is not None else self._char_limit(target)
        return self._consolidation_failure({
            "success": False,
            "error": message + " No operations were applied (batch is all-or-nothing).",
            "current_size": current,
            "max_size": effective_limit,
            "usage": f"{current:,}/{effective_limit:,}",
        })


    def resolve_entry(self, target: str, old_text: str, verb: str) -> dict[str, Any]:
        """Full entry *old_text* selects now, under the lock, or the error a direct edit would return."""
        old_text = (old_text or "").strip()
        with self._file_lock(self._path_for(target)):
            bak = self._reload_target(target, skip_drift=True)
            if bak is _READ_FAILED:
                return _read_failed_error(self._path_for(target))
            entries = self._entries_for(target)
            idx, loc_err = _locate_stored(entries, old_text, None)
            if loc_err:
                return loc_err
            if idx is None:
                return self._consolidation_failure({
                    "success": False,
                    "error": (
                        f"No entry matched '{old_text}'. Check current_entries below and retry with the "
                        f"exact text of the entry you want to {verb}."
                    ),
                    "current_entries": entries,
                })
            return {"success": True, "matched_entry": entries[idx]}

    def resolve_batch_entries(self, target: str, operations: list[dict[str, Any]]) -> dict[str, Any]:
        """Dry-run ``apply_batch`` under the lock without persisting: the same content scan,
        op walk, empty-store and budget checks, so it fails exactly where the direct batch
        would; on success ``{"success": True, "matched_entries": [...]}`` — per op, the FULL
        entry its replace/remove selects now (None for add), in batch order."""
        return self._batch(target, operations, commit=False)

    def format_for_system_prompt(self, target: str) -> Optional[str]:
        """
        Return the frozen snapshot for system prompt injection.

        This returns the state captured at load_from_disk() time, NOT the live
        state. Mid-session writes do not affect this. This keeps the system
        prompt stable across all turns, preserving the prefix cache.

        Returns None if the snapshot is empty (no entries at load time).
        """
        block = self._system_prompt_snapshot.get(target, "")
        return block if block else None

    # -- Internal helpers --

    @staticmethod
    def _previews(entries: list[str], width: int = 80) -> list[str]:
        """Truncated one-line previews of entries for error feedback."""
        return [e[:width] + ("..." if len(e) > width else "") for e in entries]

    def _success_response(
        self, target: str, message: str | None = None, limit: Optional[int] = None, **extra
    ) -> dict[str, Any]:
        # A successful write means the consolidation loop made progress, so the
        # per-turn failure budget resets (the cap counts consecutive failures,
        # not lifetime ones within a turn) (#42405).
        self._consolidation_failures = 0
        entries = self._entries_for(target)
        current = self._char_count(target)
        effective_limit = limit if limit is not None else self._char_limit(target)
        pct = (
            min(100, int((current / effective_limit) * 100))
            if effective_limit > 0
            else 0
        )

        # The success response is intentionally TERMINAL: it confirms the write
        # landed and tells the model to stop. We do NOT echo the full entries
        # list here -- dumping it invites the model to "find more to fix" and
        # re-issue the same operations (observed thrash: the correct batch on
        # call 1, then 5 redundant repeats). Entries are only shown on the
        # error/over-budget paths, where the model genuinely needs them to
        # decide what to consolidate.
        resp = {
            "success": True,
            "done": True,
            "target": target,
            "usage": f"{pct}% — {current:,}/{effective_limit:,} chars",
            "entry_count": len(entries),
        }
        if message:
            resp["message"] = message
        if extra:
            resp.update(extra)
        resp["note"] = "Write saved. This update is complete — do not repeat it."
        return resp

    def _render_block(self, target: str, entries: list[str]) -> str:
        """System prompt block: header + usage indicator + entries ("" when empty)."""
        if not entries:
            return ""

        limit = self._char_limit(target)
        content = ENTRY_DELIMITER.join(entries)
        # Annotation overhead is prompt context, not persisted memory budget.
        current = self._char_count(target)
        pct = min(100, int((current / limit) * 100)) if limit > 0 else 0

        if target == "user":
            header = (
                f"{MEMORY_BLOCK_HEADERS['user']} [{pct}% — {current:,}/{limit:,} chars]"
            )
        else:
            header = f"{MEMORY_BLOCK_HEADERS['memory']} [{pct}% — {current:,}/{limit:,} chars]"

        separator = "═" * 46
        return f"{separator}\n{header}\n{separator}\n{content}"

    @staticmethod
    def _read_raw_checked(path: Path) -> tuple[str, bool]:
        """``(raw, read_ok)``; ``read_ok`` is False ONLY when the file EXISTS but can't be
        read. Decoding stays STRICT (``errors="replace"`` would hand callers a lossy view
        a save then persists); ``utf-8-sig`` strips a Notepad BOM off the first entry."""
        if not path.exists():
            return "", True
        try:
            # utf-8-sig strips a leading UTF-8 BOM (Notepad-edited memory
            # files on Windows) and is byte-identical to utf-8 otherwise.
            # Plain utf-8 kept U+FEFF glued to the first entry, corrupting
            # matching/dedup for that entry forever (#10878 / PR #10888).
            # Decode errors stay STRICT on purpose: errors="replace" would
            # hand read-modify-write callers a lossy view that a subsequent
            # save persists over the real bytes — the wipe class documented
            # above. Undecodable bytes must surface as read_ok=False.
            return path.read_text(encoding="utf-8-sig"), True
        except (OSError, UnicodeDecodeError):
            return "", False

    @staticmethod
    def _parse_entries(raw: str) -> list[str]:
        """Split raw memory-file text into stripped, non-empty entries."""
        if not raw.strip():
            return []
        # Use ENTRY_DELIMITER for consistency with _write_file. Splitting by "§"
        # alone would incorrectly split entries that contain "§" in their content.
        entries = [e.strip() for e in raw.split(ENTRY_DELIMITER)]
        return [e for e in entries if e]

    @staticmethod
    def _read_entries_checked(path: Path) -> tuple[list[str], bool]:
        """Read + parse a memory file, distinguishing unreadable from empty.

        Returns ``(entries, read_ok)`` — see ``_read_raw_checked`` for the
        ``read_ok`` contract.
        """
        raw, read_ok = MemoryStore._read_raw_checked(path)
        if not read_ok:
            return [], False
        return MemoryStore._parse_entries(raw), True

    @staticmethod
    def _read_file(path: Path) -> list[str]:
        """Read a memory file and split into entries (empty list on any error).

        Retained for read-only callers (``load_from_disk``) that build in-memory
        state without persisting; a failed read degrading to ``[]`` there is
        harmless because nothing is written back. Read-modify-write paths use
        ``_read_raw_checked`` so they can refuse to overwrite an unreadable
        file — see ``_reload_target``.
        """
        return MemoryStore._read_entries_checked(path)[0]

    def _detect_external_drift(self, target: str, raw: str) -> Optional[str]:
        """``.bak.<ts>`` snapshot path if *raw* shows external drift, else None. The one
        signal is an entry over the whole-file limit: no tool-written entry can be, so it is
        free-form text appended without delimiters, and replacing that "entry" would discard
        it. Layout differences (blank lines around ``§``, empty entries, padding) are NOT
        drift: parsing only strips whitespace, so the flush loses nothing but whitespace, and
        refusing there blocked every replace/remove on a hand-formatted file for good (#107270)."""
        if max(map(len, self._parse_entries(raw)), default=0) <= self._char_limit(target):
            return None
        path = self._path_for(target)
        bak_path = path.with_suffix(path.suffix + f".bak.{int(time.time())}")
        try:
            bak_path.write_text(raw, encoding="utf-8")
        except OSError:
            return str(bak_path) + " (BACKUP FAILED — file unchanged on disk)"
        return str(bak_path)

    @staticmethod
    def _write_file(path: Path, entries: list[str]):
        """Atomic temp-file + rename so readers never see a truncated file.

        open("w") + flock truncates before the lock is acquired, racing
        concurrent readers. Callers hold ``_file_lock``: a bare write from an
        earlier snapshot drops concurrent entries (#119668).
        """
        content = ENTRY_DELIMITER.join(entries) if entries else ""
        try:
            atomic_write_text(path, content, tmp_prefix=".mem_")
        except OSError as e:
            raise RuntimeError(f"Failed to write memory file {path}: {e}")


def load_on_disk_store() -> "MemoryStore":
    """Build a fresh on-disk :class:`MemoryStore`, honoring configured char limits.

    Use this from any context that has no live agent (the messaging gateway, the
    Desktop GUI, the bare CLI ``/memory`` handler) but still needs to read or
    apply approved memory writes. Mirrors how the live agent constructs its store
    in ``agent/agent_init.py`` — including the user's ``memory.memory_char_limit``
    / ``memory.user_char_limit`` overrides — so an approval applied without a live
    agent enforces the SAME caps as one applied with one.

    Falls back to the built-in defaults if config can't be loaded, so this can
    never raise on a missing/unreadable config.
    """
    from tools.memory_tool import get_builtin_memory_config, get_builtin_memory_store_flags
    memory_char_limit = DEFAULT_MEMORY_CHAR_LIMIT
    user_char_limit = DEFAULT_USER_CHAR_LIMIT
    memory_enabled = True
    user_profile_enabled = True
    allow_batch_override = False
    try:
        from hermes_cli.config import load_config

        config = load_config() or {}
        mem_cfg = get_builtin_memory_config(config)
        memory_enabled, user_profile_enabled = get_builtin_memory_store_flags(config)
        memory_char_limit = int(mem_cfg.get("memory_char_limit", memory_char_limit))
        user_char_limit = int(mem_cfg.get("user_char_limit", user_char_limit))
        allow_batch_override = bool(
            mem_cfg.get("allow_batch_memory_char_limit_override", False)
        )
    except Exception:
        pass  # config optional - fall back to defaults rather than break /memory

    store = MemoryStore(
        memory_char_limit=memory_char_limit,
        user_char_limit=user_char_limit,
        allow_batch_override=allow_batch_override,
        memory_enabled=memory_enabled,
        user_profile_enabled=user_profile_enabled,
    )
    store.load_from_disk()
    return store


