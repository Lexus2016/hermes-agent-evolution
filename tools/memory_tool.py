#!/usr/bin/env python3
"""
Memory Tool Module - Persistent Curated Memory

Provides bounded, file-backed memory that persists across sessions. Two stores:
  - MEMORY.md: agent's personal notes and observations (environment facts, project
    conventions, tool quirks, things learned)
  - USER.md: what the agent knows about the user (preferences, communication style,
    expectations, workflow habits)

Both are injected into the system prompt as a frozen snapshot at session start.
Mid-session writes update files on disk immediately (durable) but do NOT change
the system prompt -- this preserves the prefix cache for the entire session.
The snapshot refreshes on the next session start.

Entry delimiter: § (section sign). Entries can be multiline.
Character limits (not tokens) because char counts are model-independent.

Design:
- Single `memory` tool with action parameter: add, replace, remove
- replace/remove use short unique substring matching (not full text or IDs)
- Behavioral guidance lives in the tool schema description
- Frozen snapshot pattern: system prompt is stable, tool responses show live state
"""

import json
import logging
import os
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from hermes_constants import get_hermes_home
from typing import Dict, Any, List, Optional

from utils import atomic_replace

# fcntl is Unix-only; on Windows use msvcrt for file locking
msvcrt = None
try:
    import fcntl
except ImportError:
    fcntl = None
    try:
        import msvcrt
    except ImportError:
        pass

logger = logging.getLogger(__name__)


# Where memory files live — resolved dynamically so profile overrides
# (HERMES_HOME env var changes) are always respected.  The old module-level
# constant was cached at import time and could go stale if a profile switch
# happened after the first import.
def get_memory_dir() -> Path:
    """Return the profile-scoped memories directory."""
    return get_hermes_home() / "memories"


ENTRY_DELIMITER = "\n§\n"


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


def _scan_memory_content(content: str) -> Optional[str]:
    """Scan memory content for injection/exfil patterns. Returns error string if blocked."""
    return _first_threat_message(content, scope="strict")


def _make_provenance(source_class: str, trust_tier: str):
    """Build a guard ``Provenance`` from the entry's source class + trust tier.

    Imported lazily so ``tools.memory_tool`` keeps no hard dependency on the
    optional guard module (the default-off path never touches it).
    """
    from agent.memory_guard import Provenance

    return Provenance(source_class=source_class, trust_tier=trust_tier)


def _log_guard_event(action: str, target: str, event: Dict[str, Any]) -> None:
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


def _drift_error(path: "Path", bak_path: str) -> Dict[str, Any]:
    """Build the error dict returned when external drift is detected.

    The on-disk memory file contains content that wouldn't round-trip
    through the tool's parser/serializer — flushing would discard the
    appended/edited content from a patch tool, shell append, manual edit,
    or sister-session write. We refuse the mutation, point the operator at
    the .bak.<ts> snapshot we took, and tell them what to do next.
    """
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
        memory_char_limit: int = 4000,
        user_char_limit: int = 2500,
        guard: Optional[object] = None,
        allow_batch_override: bool = False,
    ):
        self.memory_entries: List[str] = []
        self.user_entries: List[str] = []
        self.memory_char_limit = memory_char_limit
        self.user_char_limit = user_char_limit
        # Explicit opt-in for per-call dynamic limit overrides. Default False so
        # dynamic changes cannot silently alter the configured budget (issue #517).
        self.allow_batch_override = allow_batch_override
        # Frozen snapshot for system prompt -- set once at load_from_disk()
        self._system_prompt_snapshot: Dict[str, str] = {"memory": "", "user": ""}
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

    def reset_consolidation_failures(self) -> None:
        """Reset the per-turn consolidation-failure counter (call at turn start)."""
        self._consolidation_failures = 0

    def _consolidation_failure(self, response: Dict[str, Any]) -> Dict[str, Any]:
        """Count an at-capacity consolidation failure and degrade gracefully.

        Under the per-turn cap, return ``response`` unchanged (it already tells
        the model how to self-correct + retry in this turn). Once the cap is
        exceeded, drop the retry instruction and return a TERMINAL result so the
        model stops looping memory calls and proceeds to answer the user — a
        failed memory side effect must never block the turn's reply (#42405).
        """
        self._consolidation_failures += 1
        if self._consolidation_failures <= self._MAX_CONSOLIDATION_FAILURES_PER_TURN:
            return response
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
        sanitized_memory = self._sanitize_entries_for_snapshot(
            self.memory_entries, "MEMORY.md"
        )
        sanitized_user = self._sanitize_entries_for_snapshot(
            self.user_entries, "USER.md"
        )

        # Capture frozen snapshot for system prompt injection
        self._system_prompt_snapshot = {
            "memory": self._render_block("memory", sanitized_memory),
            "user": self._render_block("user", sanitized_user),
        }

    @staticmethod
    def _sanitize_entries_for_snapshot(entries: List[str], filename: str) -> List[str]:
        """Return ``entries`` with any threat-matching entry replaced by a placeholder.

        Each entry is scanned with the shared threat-pattern library at the
        ``"strict"`` scope (same as memory writes).  On match, the entry is
        replaced in the returned list with ``"[BLOCKED: <filename> entry
        contained threat pattern: <ids>. Removed from system prompt.]"`` —
        the placeholder enters the snapshot, the original entry stays in
        live state for the user to inspect and delete.

        Empty or already-block-marker entries pass through unchanged.

        Provenance trailers (#316) are stripped before rendering: the snapshot
        shows the clean display text, never the ``⟦src:…⟧`` sentinel. The scan
        still runs over the raw entry so threat detection is unaffected, and
        untagged legacy entries render byte-identically to before.
        """
        from tools.threat_patterns import scan_for_threats

        sanitized: List[str] = []
        for entry in entries:
            if not entry or entry.startswith("[BLOCKED:"):
                sanitized.append(entry)
                continue
            findings = scan_for_threats(entry, scope="strict")
            if findings:
                logger.warning(
                    "Memory entry from %s blocked at load time: %s",
                    filename,
                    ", ".join(findings),
                )
                sanitized.append(
                    f"[BLOCKED: {filename} entry contained threat pattern(s): "
                    f"{', '.join(findings)}. Removed from system prompt; "
                    f"use memory(action=remove) "
                    f"to delete the original.]"
                )
            else:
                # Render clean display text (no provenance trailer). For
                # untagged entries this is the entry verbatim.
                sanitized.append(parse_provenance(entry)[0])
        return sanitized

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
        lock_path.parent.mkdir(parents=True, exist_ok=True)

        if fcntl is None and msvcrt is None:
            yield
            return

        fd = open(lock_path, "a+", encoding="utf-8")
        try:
            if fcntl:
                MemoryStore._acquire_fcntl_lock(fd)
            else:
                fd.seek(0)
                msvcrt.locking(fd.fileno(), msvcrt.LK_LOCK, 1)
            yield
        finally:
            if fcntl:
                try:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                except (OSError, IOError):
                    pass
            elif msvcrt:
                try:
                    fd.seek(0)
                    msvcrt.locking(fd.fileno(), msvcrt.LK_UNLCK, 1)
                except (OSError, IOError):
                    pass
            fd.close()

    @staticmethod
    def _acquire_fcntl_lock(fd, timeout: Optional[float] = None) -> None:
        """Acquire an exclusive flock with bounded retry and backoff.

        Uses LOCK_EX | LOCK_NB (non-blocking) in a retry loop with
        exponential backoff (0.05s, 0.1s, 0.2s, ...). Raises TimeoutError
        if the lock isn't acquired within *timeout* seconds.
        """
        timeout = timeout if timeout is not None else MemoryStore._LOCK_TIMEOUT_SECONDS
        deadline = time.monotonic() + timeout
        wait = 0.05
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return  # acquired
            except (OSError, IOError):
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

    def _reload_target(self, target: str, *, skip_drift: bool = False) -> Optional[str]:
        """Re-read entries from disk into in-memory state.

        Called under file lock to get the latest state before mutating.
        Returns the backup path if external drift was detected (the on-disk
        file contains content that wouldn't round-trip through our
        parser/serializer, OR an entry larger than the store's char limit).
        When drift is detected the caller must abort the mutation —
        flushing would discard the un-roundtrippable content.
        Returns None on clean reload.

        When *skip_drift* is True the round-trip / entry-size check is
        bypassed.  Used by the ``add`` action which appends without
        rewriting, so existing content is never clobbered.
        """
        path = self._path_for(target)
        bak = None if skip_drift else self._detect_external_drift(target)
        fresh = self._read_file(path)
        fresh = list(dict.fromkeys(fresh))  # deduplicate
        self._set_entries(target, fresh)
        return bak

    def save_to_disk(self, target: str):
        """Persist entries to the appropriate file. Called after every mutation."""
        get_memory_dir().mkdir(parents=True, exist_ok=True)
        self._write_file(self._path_for(target), self._entries_for(target))

    def _entries_for(self, target: str) -> List[str]:
        if target == "user":
            return self.user_entries
        return self.memory_entries

    def _set_entries(self, target: str, entries: List[str]):
        if target == "user":
            self.user_entries = entries
        else:
            self.memory_entries = entries

    def _char_count(self, target: str) -> int:
        entries = self._entries_for(target)
        if not entries:
            return 0
        return len(ENTRY_DELIMITER.join(entries))

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
    ) -> Dict[str, Any]:
        """Append a new entry. Returns error if it would exceed the char limit.

        ``source_class`` / ``trust_tier`` tag the entry's provenance (#316).
        When both are the safe defaults the entry is stored verbatim (no
        trailer) so the on-disk format is byte-identical to pre-#316.
        """
        content = content.strip()
        if not content:
            return {"success": False, "error": "Content cannot be empty."}

        prov_error = _validate_provenance(source_class, trust_tier)
        if prov_error:
            return {"success": False, "error": prov_error}

        # Scan the user-visible content (not the provenance trailer) for
        # injection/exfiltration before accepting. With no guard configured
        # (default) this is the pre-#315 binary block; a configured guard may
        # instead warn (store as-is) or strip (store the excised content).
        if self._guard is not None:
            self._last_provenance = _make_provenance(source_class, trust_tier)
        scan_error, content, guard_event = self._gate_write(content)
        if scan_error:
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
            self._reload_target(target, skip_drift=True)

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
                current = self._char_count(target)
                return self._consolidation_failure({
                    "success": False,
                    "error": (
                        f"Memory at {current:,}/{limit:,} chars. "
                        f"Adding this entry ({len(content)} chars) would exceed the limit. "
                        f"Consolidate now: use 'replace' to merge overlapping entries into "
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
    ) -> Dict[str, Any]:
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
            if bak:
                return _drift_error(self._path_for(target), bak)

            entries = self._entries_for(target)
            matches = [
                (i, e)
                for i, e in enumerate(entries)
                if old_text in parse_provenance(e)[0]
            ]

            if not matches:
                return self._consolidation_failure({
                    "success": False,
                    "error": f"No entry matched '{old_text}'. Check current_entries below and retry with the exact text of the entry you want to replace.",
                    "current_entries": entries,
                })

            if len(matches) > 1:
                # If all matches are identical (exact duplicates), operate on the first one
                unique_texts = {e for _, e in matches}
                if len(unique_texts) > 1:
                    previews = self._previews(
                        [parse_provenance(e)[0] for _, e in matches]
                    )
                    return {
                        "success": False,
                        "error": f"Multiple entries matched '{old_text}'. Be more specific.",
                        "matches": previews,
                    }
                # All identical -- safe to replace just the first

            idx = matches[0][0]
            limit = self._char_limit(target)

            # Check that replacement doesn't blow the budget
            test_entries = entries.copy()
            test_entries[idx] = stored_new
            new_total = len(ENTRY_DELIMITER.join(test_entries))

            if new_total > limit:
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

        return self._success_response(target, "Entry replaced.")

    def remove(self, target: str, old_text: str) -> Dict[str, Any]:
        """Remove the entry containing old_text substring."""
        old_text = old_text.strip()
        if not old_text:
            return {"success": False, "error": "old_text cannot be empty."}

        with self._file_lock(self._path_for(target)):
            bak = self._reload_target(target)
            if bak:
                return _drift_error(self._path_for(target), bak)

            entries = self._entries_for(target)
            matches = [
                (i, e)
                for i, e in enumerate(entries)
                if old_text in parse_provenance(e)[0]
            ]

            if not matches:
                return self._consolidation_failure({
                    "success": False,
                    "error": f"No entry matched '{old_text}'. Check current_entries below and retry with the exact text of the entry you want to remove.",
                    "current_entries": entries,
                })

            if len(matches) > 1:
                # If all matches are identical (exact duplicates), remove the first one
                unique_texts = {e for _, e in matches}
                if len(unique_texts) > 1:
                    previews = self._previews(
                        [parse_provenance(e)[0] for _, e in matches]
                    )
                    return {
                        "success": False,
                        "error": f"Multiple entries matched '{old_text}'. Be more specific.",
                        "matches": previews,
                    }
                # All identical -- safe to remove just the first

            idx = matches[0][0]
            entries.pop(idx)
            self._set_entries(target, entries)
            self.save_to_disk(target)

        return self._success_response(target, "Entry removed.")

    def compact(
        self,
        target: str,
        target_size: int = None,
        prefer: str = "longest",
    ) -> Dict[str, Any]:
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
                return self._success_response(
                    target,
                    message=f"Memory already fits ({start_total:,} chars ≤ {goal:,}). No compaction needed.",
                )

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
                # Trim the entry: keep at least one sentence/clause and up to
                # half of the original text, removing from the end.
                min_keep = max(20, len(text) // 2)
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
    ) -> List[Dict[str, Any]]:
        """Return live entries as provenance-resolved rows, optionally filtered.

        Each row is ``{"text": <display>, "source_class": ..., "trust_tier": ...}``.
        Entries with no provenance trailer (legacy + default adds) resolve to
        the safe defaults. With NO filters this returns every entry in order —
        the no-filter call is the byte-compatible "read everything" path.

        Filters (#316 retrieval-time selection — tagging only, no enforcement):
          * ``source_filter``: a source_class string or iterable of them; keep
            entries whose source_class is in the set.
          * ``min_trust``: a trust tier; keep entries whose tier ranks >= it.
        """
        if isinstance(source_filter, str):
            allowed = {source_filter}
        elif source_filter is None:
            allowed = None
        else:
            allowed = set(source_filter)

        min_rank = _trust_rank(min_trust) if min_trust is not None else None

        rows: List[Dict[str, Any]] = []
        for entry in self._entries_for(target):
            text, src, tier = parse_provenance(entry)
            if allowed is not None and src not in allowed:
                continue
            if min_rank is not None and _trust_rank(tier) < min_rank:
                continue
            rows.append({"text": text, "source_class": src, "trust_tier": tier})
        return rows

    def apply_batch(
        self,
        target: str,
        operations: List[Dict[str, Any]],
        memory_char_limit: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Apply a sequence of add/replace/remove ops to one target atomically.

        All operations are validated and applied against the FINAL budget --
        intermediate overflow is irrelevant. This lets the model free space
        (remove/replace) and add new entries in a SINGLE tool call instead of
        the multi-turn consolidate-then-retry dance that re-sends the whole
        conversation context several times.

        Semantics: all-or-nothing. If any op is malformed, doesn't match, or
        the net result would exceed the char limit, NOTHING is written and an
        error is returned describing the first failure plus the live state.

        ``memory_char_limit`` is an optional per-call override for the 'memory'
        target only. It is ignored unless ``self.allow_batch_override`` is True,
        which keeps the configured budget the default and prevents dynamic
        overrides from silently changing behavior (issue #517). The frozen
        system-prompt snapshot always uses the configured limit, so a one-off
        override cannot invalidate the per-conversation prompt cache.
        """
        if not operations:
            return {"success": False, "error": "operations list is empty."}

        # Scan every add/replace content for injection/exfil BEFORE touching
        # disk -- a single poisoned op rejects the whole batch.
        for i, op in enumerate(operations):
            act = (op or {}).get("action")
            new_content = (op or {}).get("content")
            if act in {"add", "replace"} and new_content:
                scan_error = _scan_memory_content(new_content)
                if scan_error:
                    return {
                        "success": False,
                        "error": f"Operation {i + 1}: {scan_error}",
                    }

        with self._file_lock(self._path_for(target)):
            bak = self._reload_target(target)
            if bak:
                return _drift_error(self._path_for(target), bak)

            # Work on a copy; only commit if the whole batch validates.
            working: List[str] = list(self._entries_for(target))
            limit = self._char_limit(target, dynamic_limit=memory_char_limit)

            for i, op in enumerate(operations):
                op = op or {}
                act = op.get("action")
                content = (op.get("content") or "").strip()
                old_text = (op.get("old_text") or "").strip()
                pos = f"Operation {i + 1} ({act or 'unknown'})"

                if act == "add":
                    if not content:
                        return self._batch_error(target, f"{pos}: content is required.", limit=limit)
                    if content in working:
                        continue  # idempotent -- skip duplicate, don't fail the batch
                    working.append(content)

                elif act == "replace":
                    if not old_text:
                        return self._batch_error(
                            target, f"{pos}: old_text is required.", limit=limit
                        )
                    if not content:
                        return self._batch_error(
                            target,
                            f"{pos}: content is required (use action='remove' to delete).",
                            limit=limit,
                        )
                    matches = [j for j, e in enumerate(working) if old_text in e]
                    if not matches:
                        return self._batch_error(
                            target, f"{pos}: no entry matched '{old_text}'.", limit=limit
                        )
                    if len({working[j] for j in matches}) > 1:
                        return self._batch_error(
                            target,
                            f"{pos}: '{old_text}' matched multiple distinct entries -- be more specific.",
                            limit=limit,
                        )
                    working[matches[0]] = content

                elif act == "remove":
                    if not old_text:
                        return self._batch_error(
                            target, f"{pos}: old_text is required.", limit=limit
                        )
                    matches = [j for j, e in enumerate(working) if old_text in e]
                    if not matches:
                        return self._batch_error(
                            target, f"{pos}: no entry matched '{old_text}'.", limit=limit
                        )
                    if len({working[j] for j in matches}) > 1:
                        return self._batch_error(
                            target,
                            f"{pos}: '{old_text}' matched multiple distinct entries -- be more specific.",
                            limit=limit,
                        )
                    working.pop(matches[0])

                else:
                    return self._batch_error(
                        target,
                        f"{pos}: unknown action. Use add, replace, or remove.",
                        limit=limit,
                    )

            # Budget check against the FINAL state only.
            new_total = len(ENTRY_DELIMITER.join(working)) if working else 0
            if new_total > limit:
                current = self._char_count(target)
                return self._consolidation_failure({
                    "success": False,
                    "error": (
                        f"After applying all {len(operations)} operations, memory would be at "
                        f"{new_total:,}/{limit:,} chars -- over the limit. Remove or shorten more "
                        f"entries in the same batch (see current_entries below), then retry."
                    ),
                    "current_entries": self._entries_for(target),
                    "current_size": current,
                    "max_size": limit,
                    "would_be_size": new_total,
                    "usage": f"{current:,}/{limit:,}",
                })

            # Commit.
            self._set_entries(target, working)
            self.save_to_disk(target)

        return self._success_response(
            target, f"Applied {len(operations)} operation(s).", limit=limit
        )

    def _batch_error(self, target: str, message: str, limit: Optional[int] = None) -> Dict[str, Any]:
        """Build a batch-abort error that reports live (uncommitted) state."""
        current = self._char_count(target)
        effective_limit = limit if limit is not None else self._char_limit(target)
        return self._consolidation_failure({
            "success": False,
            "error": message + " No operations were applied (batch is all-or-nothing).",
            "current_entries": self._entries_for(target),
            "current_size": current,
            "max_size": effective_limit,
            "usage": f"{current:,}/{effective_limit:,}",
        })

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
    def _previews(entries: List[str], width: int = 80) -> List[str]:
        """Truncated one-line previews of entries for error feedback."""
        return [e[:width] + ("..." if len(e) > width else "") for e in entries]

    def _success_response(self, target: str, message: str = None, limit: Optional[int] = None) -> Dict[str, Any]:
        # A successful write means the consolidation loop made progress, so the
        # per-turn failure budget resets (the cap counts consecutive failures,
        # not lifetime ones within a turn) (#42405).
        self._consolidation_failures = 0
        entries = self._entries_for(target)
        current = self._char_count(target)
        effective_limit = limit if limit is not None else self._char_limit(target)
        pct = min(100, int((current / effective_limit) * 100)) if effective_limit > 0 else 0

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
        resp["note"] = "Write saved. This update is complete — do not repeat it."
        return resp

    def _render_block(self, target: str, entries: List[str]) -> str:
        """Render a system prompt block with header and usage indicator."""
        if not entries:
            return ""

        limit = self._char_limit(target)
        content = ENTRY_DELIMITER.join(entries)
        current = len(content)
        pct = min(100, int((current / limit) * 100)) if limit > 0 else 0

        if target == "user":
            header = (
                f"USER PROFILE (who the user is) [{pct}% — {current:,}/{limit:,} chars]"
            )
        else:
            header = (
                f"MEMORY (your personal notes) [{pct}% — {current:,}/{limit:,} chars]"
            )

        separator = "═" * 46
        return f"{separator}\n{header}\n{separator}\n{content}"

    @staticmethod
    def _read_file(path: Path) -> List[str]:
        """Read a memory file and split into entries.

        No file locking needed: _write_file uses atomic rename, so readers
        always see either the previous complete file or the new complete file.
        """
        if not path.exists():
            return []
        try:
            raw = path.read_text(encoding="utf-8")
        except (OSError, IOError):
            return []

        if not raw.strip():
            return []

        # Use ENTRY_DELIMITER for consistency with _write_file. Splitting by "§"
        # alone would incorrectly split entries that contain "§" in their content.
        entries = [e.strip() for e in raw.split(ENTRY_DELIMITER)]
        return [e for e in entries if e]

    def _detect_external_drift(self, target: str) -> Optional[str]:
        """Return a backup-path string if on-disk content shows external drift.

        The memory file is supposed to be a list of small entries the tool
        wrote, joined by §. Detect drift via two signals:

        1. Round-trip mismatch — re-parsing and re-serializing the file
           doesn't produce identical bytes (rare; would catch oddly-encoded
           delimiters).
        2. Entry-size overflow — any single parsed entry exceeds the
           store's whole-file char limit. The tool budgets the ENTIRE store
           against that limit; no single tool-written entry can exceed it.
           When we see one entry larger than the limit, an external writer
           (patch tool, shell append, manual edit, sister session) appended
           free-form content into what the tool will treat as one entry.
           Flushing would then truncate that entry to the model's new
           content, discarding the appended bytes — issue #26045.

        Returns the absolute path of the .bak file when drift was found and
        backed up; returns None when the file looks tool-shaped.

        Note: this is an INSTANCE method (not static) because we need the
        per-target char_limit for signal #2.
        """
        path = self._path_for(target)
        if not path.exists():
            return None
        try:
            raw = path.read_text(encoding="utf-8")
        except (OSError, IOError):
            return None
        if not raw.strip():
            return None

        parsed = [e.strip() for e in raw.split(ENTRY_DELIMITER) if e.strip()]
        roundtrip = ENTRY_DELIMITER.join(parsed)

        char_limit = self._char_limit(target)
        max_entry_len = max((len(e) for e in parsed), default=0)

        drift_detected = (raw.strip() != roundtrip) or (max_entry_len > char_limit)
        if not drift_detected:
            return None

        # Drift confirmed — snapshot the file so the operator can recover
        # whatever the external writer added, then return the .bak path so
        # the caller can refuse the mutation.
        ts = int(time.time())
        bak_path = path.with_suffix(path.suffix + f".bak.{ts}")
        try:
            bak_path.write_text(raw, encoding="utf-8")
        except (OSError, IOError):
            return str(bak_path) + " (BACKUP FAILED — file unchanged on disk)"
        return str(bak_path)

    @staticmethod
    def _write_file(path: Path, entries: List[str]):
        """Write entries to a memory file using atomic temp-file + rename.

        Previous implementation used open("w") + flock, but "w" truncates the
        file *before* the lock is acquired, creating a race window where
        concurrent readers see an empty file. Atomic rename avoids this:
        readers always see either the old complete file or the new one.
        """
        content = ENTRY_DELIMITER.join(entries) if entries else ""
        try:
            # Write to temp file in same directory (same filesystem for atomic rename)
            fd, tmp_path = tempfile.mkstemp(
                dir=str(path.parent), suffix=".tmp", prefix=".mem_"
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(content)
                    f.flush()
                    os.fsync(f.fileno())
                atomic_replace(tmp_path, path)
            except BaseException:
                # Clean up temp file on any failure
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
                raise
        except (OSError, IOError) as e:
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
    memory_char_limit = 2200
    user_char_limit = 1375
    allow_batch_override = False
    try:
        from hermes_cli.config import load_config

        mem_cfg = (load_config() or {}).get("memory", {}) or {}
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
    )
    store.load_from_disk()
    return store


def _apply_write_gate(
    action: str,
    target: str,
    content: Optional[str],
    old_text: Optional[str],
    source_class: str = DEFAULT_SOURCE_CLASS,
    trust_tier: str = DEFAULT_TRUST_TIER,
) -> Optional[str]:
    """Evaluate the memory write gate. Returns a JSON tool-result string when
    the write should NOT proceed normally (blocked or staged), or None when the
    caller should perform the real write.

    Only the mutating actions (add/replace/remove) are gated. Provenance tags
    (#316) ride along in the staged payload so an approved write keeps them.
    """
    if action not in {"add", "replace", "remove"}:
        return None

    try:
        from tools import write_approval as wa
    except Exception:
        # If the gate module can't load, fail open (current behaviour) rather
        # than blocking all memory writes.
        return None

    # Build a small inline summary/detail for the foreground approval prompt.
    label = "user profile" if target == "user" else "memory"
    if action == "add":
        summary = f"add to {label}"
        detail = content or ""
    elif action == "replace":
        summary = f"replace in {label}"
        detail = f"old: {old_text}\nnew: {content}"
    else:  # remove
        summary = f"remove from {label}"
        detail = old_text or ""

    decision = wa.evaluate_gate(wa.MEMORY, inline_summary=summary, inline_detail=detail)

    if decision.allow:
        return None

    if decision.blocked:
        return tool_error(decision.message, success=False)

    # stage
    payload = {
        "action": action,
        "target": target,
        "content": content,
        "old_text": old_text,
        "source_class": source_class,
        "trust_tier": trust_tier,
    }
    record = wa.stage_write(
        wa.MEMORY,
        payload,
        summary=f"{summary}: {detail[:120]}",
        origin=wa.current_origin(),
    )
    return json.dumps(
        {
            "success": True,
            "staged": True,
            "pending_id": record["id"],
            "message": decision.message,
        },
        ensure_ascii=False,
    )


def _apply_batch_write_gate(
    target: str, operations: List[Dict[str, Any]]
) -> Optional[str]:
    """Evaluate the write gate for a batch of memory operations.

    Returns a JSON tool-result string when the batch should NOT proceed
    (blocked or staged), or None when the caller should perform the real
    batch write. The whole batch is gated as a single unit.
    """
    try:
        from tools import write_approval as wa
    except Exception:
        return None

    label = "user profile" if target == "user" else "memory"
    summary = f"apply {len(operations)} op(s) to {label}"
    detail_lines = []
    for op in operations:
        op = op or {}
        act = op.get("action", "?")
        if act == "remove":
            detail_lines.append(f"- remove: {op.get('old_text', '')}")
        elif act == "replace":
            detail_lines.append(
                f"- replace: {op.get('old_text', '')} -> {op.get('content', '')}"
            )
        else:
            detail_lines.append(f"- {act}: {op.get('content', '')}")
    detail = "\n".join(detail_lines)

    decision = wa.evaluate_gate(wa.MEMORY, inline_summary=summary, inline_detail=detail)

    if decision.allow:
        return None

    if decision.blocked:
        return tool_error(decision.message, success=False)

    payload = {"action": "batch", "target": target, "operations": operations}
    record = wa.stage_write(
        wa.MEMORY,
        payload,
        summary=f"{summary}: {detail[:120]}",
        origin=wa.current_origin(),
    )
    return json.dumps(
        {
            "success": True,
            "staged": True,
            "pending_id": record["id"],
            "message": decision.message,
        },
        ensure_ascii=False,
    )


def _missing_old_text_error(store: "MemoryStore", target: str, action: str) -> str:
    """Build a recoverable error for a replace/remove call that arrived without
    ``old_text``.

    ``replace``/``remove`` are inherently targeted -- without ``old_text`` there
    is no entry to act on, so we cannot fulfil the call. But returning a bare
    "old_text is required" is a dead-end: some structured-output clients omit the
    optional ``old_text`` field (it isn't, and can't be, schema-required without
    a top-level combinator the Codex backend rejects -- see
    tests/tools/test_memory_tool_schema.py). So instead we return the current
    entry inventory plus an explicit retry instruction, letting the model reissue
    the call with ``old_text`` set to a unique substring of the entry it means.
    Mirrors the batch path's ``_batch_error`` shape. (issues #43412, #49466)
    """
    entries = store._entries_for(target)
    current = store._char_count(target)
    limit = store._char_limit(target)
    return json.dumps(
        {
            "success": False,
            "error": (
                f"'{action}' needs old_text -- a short unique substring of the entry "
                f"to {action}. None was provided. Reissue the {action} with old_text "
                f"set to part of one of the current_entries below."
            ),
            "current_entries": entries,
            "usage": f"{current:,}/{limit:,}",
        },
        ensure_ascii=False,
    )


def memory_tool(
    action: str = None,
    target: str = "memory",
    content: str = None,
    old_text: str = None,
    source_class: str = DEFAULT_SOURCE_CLASS,
    trust_tier: str = DEFAULT_TRUST_TIER,
    source_filter: Optional[object] = None,
    min_trust: Optional[str] = None,
    operations: Optional[List[Dict[str, Any]]] = None,
    target_size: Optional[int] = None,
    prefer: str = "longest",
    memory_char_limit: Optional[int] = None,
    store: Optional[MemoryStore] = None,
) -> str:
    """
    Single entry point for the memory tool. Dispatches to MemoryStore methods.

    Two shapes:
      - Single op: action + (content / old_text).
      - Batch:     operations=[{action, content?, old_text?}, ...] applied
                   atomically against the final char budget in ONE call.
    ``source_class`` / ``trust_tier`` tag provenance on add/replace (#316).
    ``source_filter`` / ``min_trust`` filter the ``search`` action's results.
    ``memory_char_limit`` is an optional per-batch override for target='memory'
    that is only honoured when ``store.allow_batch_override`` is True (issue #517).

    Returns JSON string with results.
    """
    if store is None:
        return tool_error(
            "Memory is not available. It may be disabled in config or this environment.",
            success=False,
        )

    # Some strict providers fill optional schema fields with JSON null rather
    # than omitting them.  Treat ``target: null`` as omitted so memory writes
    # still use the documented default store instead of failing validation.
    if target is None:
        target = "memory"

    if target not in {"memory", "user"}:
        return tool_error(
            f"Invalid target '{target}'. Use 'memory' or 'user'.", success=False
        )

    # search is a read-only retrieval path — no gate, no required content.
    if action == "search":
        rows = store.search(target, source_filter=source_filter, min_trust=min_trust)
        return json.dumps(
            {
                "success": True,
                "target": target,
                "results": rows,
                "result_count": len(rows),
            },
            ensure_ascii=False,
        )

    if action == "compact":
        prefer_param = prefer if prefer is not None else "longest"
        try:
            target_size_int = int(target_size) if target_size is not None else None
        except (TypeError, ValueError):
            return tool_error(
                "target_size must be an integer number of characters.", success=False
            )
        result = store.compact(target, target_size=target_size_int, prefer=prefer_param)
        return json.dumps(result, ensure_ascii=False)

    # --- Batch path -------------------------------------------------------
    if operations:
        if not isinstance(operations, list):
            return tool_error(
                "operations must be a list of {action, content?, old_text?} objects.",
                success=False,
            )
        gate_result = _apply_batch_write_gate(target, operations)
        if gate_result is not None:
            return gate_result
        result = store.apply_batch(target, operations, memory_char_limit=memory_char_limit)
        return json.dumps(result, ensure_ascii=False)

    # --- Single-op path ---------------------------------------------------
    # Validate required params BEFORE the gate so an invalid write is rejected
    # immediately instead of being staged and only failing at approve time.
    if action == "add" and not content:
        return tool_error("Content is required for 'add' action.", success=False)
    if action == "replace" and (not old_text or not content):
        missing = "old_text" if not old_text else "content"
        if not old_text:
            # The client/model omitted old_text. Replace is inherently targeted
            # -- we can't guess which entry. Return the current inventory plus a
            # retry instruction so the model can reissue with old_text set,
            # instead of hitting a dead-end error. (issues #43412, #49466)
            return _missing_old_text_error(store, target, "replace")
        return tool_error(f"{missing} is required for 'replace' action.", success=False)
    if action == "remove" and not old_text:
        return _missing_old_text_error(store, target, "remove")

    # Approval gate: when on, stages the write (background/gateway) or prompts
    # inline (interactive CLI); when off (default) passes straight through.
    gate_result = _apply_write_gate(
        action,
        target,
        content,
        old_text,
        source_class=source_class,
        trust_tier=trust_tier,
    )
    if gate_result is not None:
        return gate_result

    if action == "add":
        result = store.add(
            target, content, source_class=source_class, trust_tier=trust_tier
        )

    elif action == "replace":
        result = store.replace(
            target, old_text, content, source_class=source_class, trust_tier=trust_tier
        )

    elif action == "remove":
        result = store.remove(target, old_text)

    else:
        return tool_error(
            f"Unknown action '{action}'. Use: add, replace, remove, search",
            success=False,
        )

    return json.dumps(result, ensure_ascii=False)


def check_memory_requirements() -> bool:
    """Memory tool has no external requirements -- always available."""
    return True


def apply_memory_pending(
    payload: Dict[str, Any], store: "MemoryStore"
) -> Dict[str, Any]:
    """Replay a staged memory write directly against the store, bypassing the
    write gate. Called by the /memory approve handler.

    Returns the store's result dict.
    """
    action = payload.get("action")
    target = payload.get("target", "memory")
    content = payload.get("content") or ""
    old_text = payload.get("old_text") or ""
    source_class = payload.get("source_class", DEFAULT_SOURCE_CLASS)
    trust_tier = payload.get("trust_tier", DEFAULT_TRUST_TIER)
    if action == "batch":
        return store.apply_batch(target, payload.get("operations") or [])
    if action == "add":
        return store.add(
            target, content, source_class=source_class, trust_tier=trust_tier
        )
    if action == "replace":
        return store.replace(
            target, old_text, content, source_class=source_class, trust_tier=trust_tier
        )
    if action == "remove":
        return store.remove(target, old_text)
    return {"success": False, "error": f"Unknown staged action '{action}'."}


# OpenAI Function-Calling Schema
# =============================================================================

MEMORY_SCHEMA = {
    "name": "memory",
    "description": (
        "Save durable facts to persistent memory that survive across sessions. Memory is "
        "injected into every future turn, so keep entries compact and high-signal.\n\n"
        "HOW: make ALL your changes in ONE call via an 'operations' array (each item: "
        "{action, content?, old_text?}). The batch applies atomically and the char limit is "
        "checked only on the FINAL result — so a single call can remove/replace stale entries "
        "to free room AND add new ones, even when an add alone would overflow. The response "
        "reports current/limit chars and confirms completion; one batch call finishes the "
        "update, so don't repeat it. Use the bare action/content/old_text fields only for a "
        "single lone change. Use action='search' to read entries back (optionally filtered "
        "by provenance via source_filter / min_trust).\n\n"
        "WHEN: save proactively when the user states a preference, correction, or personal "
        "detail, or you learn a stable fact about their environment, conventions, or workflow. "
        "Priority: user preferences & corrections > environment facts > procedures. The best "
        "memory stops the user repeating themselves.\n\n"
        "IF FULL: an add is rejected with the current entries shown. Reissue as ONE batch that "
        "removes or shortens enough stale entries and adds the new one together. Or call "
        "action='compact' first to shorten entries so the batch fits.\n\n"
        "TARGETS: 'user' = who the user is (name, role, preferences, style). 'memory' = your "
        "notes (environment, conventions, tool quirks, lessons).\n\n"
        "PROVENANCE (optional, on add/replace): tag where a fact came from. source_class = "
        "user_input (the user told you), external_tool (a tool/API returned it), agent_authored "
        "(YOUR own inference — treat as a guess), or system. trust_tier rates reliability. "
        "Tagging agent_authored guesses keeps them distinguishable from facts the user "
        "actually stated.\n\n"
        "SKIP: trivial/obvious info, easily re-discovered facts, raw data dumps, task progress, "
        "completed-work logs, temporary TODO state (use session_search for those). Reusable "
        "procedures belong in a skill, not memory."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["add", "replace", "remove", "search", "compact"],
                "description": "The action to perform (single op, or 'search' to read entries, or 'compact' to shorten entries to fit). Omit when using the 'operations' batch array.",
            },
            "target": {
                "type": "string",
                "enum": ["memory", "user"],
                "description": "Which memory store: 'memory' for personal notes, 'user' for user profile.",
            },
            "content": {
                "type": "string",
                "description": "The entry content. Required for 'add' and 'replace' (single-op shape).",
            },
            "old_text": {
                "type": "string",
                "description": "REQUIRED for 'replace' and 'remove' (single-op shape): a short unique substring identifying the existing entry to modify. Omit only for 'add'.",
            },
            "operations": {
                "type": "array",
                "description": (
                    "Batch shape: a list of operations applied atomically in one call "
                    "against the final char budget. Preferred when making multiple changes "
                    "or consolidating to make room. Each item is {action, content?, old_text?}."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "action": {
                            "type": "string",
                            "enum": ["add", "replace", "remove"],
                        },
                        "content": {
                            "type": "string",
                            "description": "Entry content for add/replace.",
                        },
                        "old_text": {
                            "type": "string",
                            "description": "Substring identifying the entry for replace/remove.",
                        },
                    },
                    "required": ["action"],
                },
            },
            "target_size": {
                "type": "integer",
                "description": "Optional for 'compact': target character count (defaults to the store limit).",
            },
            "prefer": {
                "type": "string",
                "enum": ["longest", "oldest"],
                "description": "Optional for 'compact': which entries to trim first (default: longest).",
            },
            "source_class": {
                "type": "string",
                "enum": list(SOURCE_CLASSES),
                "description": (
                    "Optional provenance for 'add'/'replace': who produced this fact. "
                    "Defaults to 'unknown'. Use 'agent_authored' for your own guesses."
                ),
            },
            "trust_tier": {
                "type": "string",
                "enum": list(TRUST_TIERS),
                "description": (
                    "Optional provenance for 'add'/'replace': how reliable this fact is. "
                    "Defaults to 'unknown'."
                ),
            },
            "source_filter": {
                "type": "array",
                "items": {"type": "string", "enum": list(SOURCE_CLASSES)},
                "description": "Optional for 'search': keep only entries with these source classes.",
            },
            "min_trust": {
                "type": "string",
                "enum": list(TRUST_TIERS),
                "description": "Optional for 'search': keep only entries at or above this trust tier.",
            },
            "memory_char_limit": {
                "type": "integer",
                "description": (
                    "Optional per-batch override for the 'memory' target char limit, "
                    "only honoured when config 'memory.allow_batch_memory_char_limit_override' "
                    "is True. Ignored for 'user' target. The system-prompt snapshot always "
                    "uses the configured limit (issue #517)."
                ),
            },
        },
        "required": ["target"],
    },
}


# --- Registry ---
from tools.registry import registry, tool_error

registry.register(
    name="memory",
    toolset="memory",
    schema=MEMORY_SCHEMA,
    handler=lambda args, **kw: memory_tool(
        action=args.get("action", ""),
        target=args.get("target", "memory"),
        content=args.get("content"),
        old_text=args.get("old_text"),
        source_class=args.get("source_class", DEFAULT_SOURCE_CLASS),
        trust_tier=args.get("trust_tier", DEFAULT_TRUST_TIER),
        source_filter=args.get("source_filter"),
        min_trust=args.get("min_trust"),
        operations=args.get("operations"),
        target_size=args.get("target_size"),
        prefer=args.get("prefer"),
        memory_char_limit=args.get("memory_char_limit"),
        store=kw.get("store"),
    ),
    check_fn=check_memory_requirements,
    emoji="🧠",
)
