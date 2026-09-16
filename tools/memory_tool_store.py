"""MemoryStore — bounded, file-backed curated memory (MEMORY.md / USER.md).
Entries are joined by ``ENTRY_DELIMITER``; budgets are in chars (model-independent).
Module state that tests monkeypatch (``get_memory_dir``, ``fcntl``/``msvcrt``) stays
in ``tools.memory_tool`` and is read lazily."""

import copy
import hashlib
import json
import logging
import os
import time
from contextlib import contextmanager, suppress
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


# Sentinel returned by ``_reload_target`` when the target file EXISTS but could
# not be read. Distinct from a drift-backup path (``str``) and from a clean
# reload (``None``): the caller must abort the mutation rather than persist over
# an unreadable file.
_READ_FAILED = object()


def _read_failed_error(path: "Path") -> Dict[str, Any]:
    """Build the error dict returned when the on-disk memory file is unreadable.

    A file that exists but cannot be read is NOT an empty store. Reading it as
    ``[]`` and then persisting would rewrite the whole file from an empty entry
    list — wiping the user's memory. We refuse the write so nothing is lost.
    """
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
        self.memory_entries: List[str] = []
        self.user_entries: List[str] = []
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

    def _failure_with_entries(self, target: str, message: str) -> Dict[str, Any]:
        """Consolidation failure carrying the live entries so the model can consolidate."""
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
        entries: List[str],
        filename: str,
        warned: Optional[set] = None,
    ) -> List[str]:
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

        warned = warned if warned is not None else set()
        sanitized: List[str] = []
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
                    f"use memory(action=remove) "
                    f"to delete the original.]"
                )
            else:
                # Render clean display text (no provenance trailer). For
                # untagged entries this is the entry verbatim.
                sanitized.append(parse_provenance(entry)[0])
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
                data = json.loads(path.read_text(encoding="utf-8"))
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

    def _evict_to_fit(
        self,
        target: str,
        new_entry: str,
        limit: int,
    ) -> Optional[List[str]]:
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
        evicted: List[str] = []

        while True:
            candidate = working + [new_entry]
            if len(ENTRY_DELIMITER.join(candidate)) <= limit:
                return evicted
            # Can't evict further without breaching the safety floor.
            if len(working) <= self.auto_evict_keep_min:
                return None
            evicted.append(parse_provenance(working.pop(0))[0])

    def _mutate(self, target: str, mutate, *, skip_drift: bool = False) -> Dict[str, Any]:
        """Lock, re-read from disk, run ``mutate(entries, limit)`` -> ``(new_entries, message)``
        or an error dict, then persist and return the success response. The reload aborts
        on an existing-but-unreadable file (even append-only ``add`` rewrites the whole
        file) and, unless *skip_drift*, on external drift (flushing would discard
        un-roundtrippable content). Drift check and parse use the SAME raw snapshot —
        a failed second read used to count as "no drift"."""
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
            return self._success_response(target, result[1])

    def _evict_replacement_to_fit(
        self,
        target: str,
        entries: List[str],
        new_text: str,
        limit: int,
    ) -> Optional[Tuple[List[str], List[str]]]:
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
        evicted: List[str] = []

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
            if bak is _READ_FAILED:
                return _read_failed_error(self._path_for(target))
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
                    previews = self._previews([
                        parse_provenance(e)[0] for _, e in matches
                    ])
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
                        "Entry replaced (evicted %d oldest entr%s to make room)."
                        % (len(evicted), "y" if len(evicted) == 1 else "ies"),
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

        return self._success_response(target, "Entry replaced.")

    def remove(self, target: str, old_text: str) -> Dict[str, Any]:
        """Remove the entry containing old_text substring."""
        old_text = old_text.strip()
        if not old_text:
            return {"success": False, "error": "old_text cannot be empty."}

        with self._file_lock(self._path_for(target)):
            bak = self._reload_target(target)
            if bak is _READ_FAILED:
                return _read_failed_error(self._path_for(target))
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
                    previews = self._previews([
                        parse_provenance(e)[0] for _, e in matches
                    ])
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
        include_superseded: bool = False,
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

        rows: List[Dict[str, Any]] = []
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
            row = {"text": text, "source_class": src, "trust_tier": tier}
            if sup_ts is not None:
                row["superseded_at"] = sup_ts
            rows.append(row)
        return rows

    def supersede(self, target: str, old_text: str) -> Dict[str, Any]:
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
            if bak is _READ_FAILED:
                return _read_failed_error(self._path_for(target))
            if bak:
                return _drift_error(self._path_for(target), bak)

            # Work on a copy; only commit if the whole batch validates.
            working: List[str] = list(self._entries_for(target))
            limit = self._char_limit(target, dynamic_limit=memory_char_limit)

            for i, op in enumerate(operations):
                op = op or {}
                act = op.get("action")
                content = (op.get("content") or op.get("new_text") or "").strip()
                old_text = (op.get("old_text") or "").strip()
                pos = f"Operation {i + 1} ({act or 'unknown'})"

                if act == "add":
                    if not content:
                        return self._batch_error(
                            target, f"{pos}: content is required.", limit=limit
                        )
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
                            target,
                            f"{pos}: no entry matched '{old_text}'.",
                            limit=limit,
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
                            target,
                            f"{pos}: no entry matched '{old_text}'.",
                            limit=limit,
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
            orig_entries = self._entries_for(target)
            if orig_entries and not working:
                # #103419: a consolidation batch that removes the last entry would
                # commit an empty file as a normal successful write. Refuse; single
                # remove() is the deliberate-wipe path.
                label = self._path_for(target).name
                return self._failure_with_entries(target, (
                    f"Refusing to empty {label}: this batch would remove every entry from a "
                    f"previously non-empty store. Nothing was applied (batch is all-or-nothing). "
                    f"Keep at least one entry — merge overlapping entries into a shorter one instead "
                    f"of removing the last one (see current_entries below). To delete the final entry "
                    f"deliberately, use single remove() calls."))
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

    def _batch_error(
        self, target: str, message: str, limit: Optional[int] = None
    ) -> Dict[str, Any]:
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

    def _success_response(
        self, target: str, message: str = None, limit: Optional[int] = None
    ) -> Dict[str, Any]:
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
                f"{MEMORY_BLOCK_HEADERS['user']} [{pct}% — {current:,}/{limit:,} chars]"
            )
        else:
            header = f"{MEMORY_BLOCK_HEADERS['memory']} [{pct}% — {current:,}/{limit:,} chars]"

        separator = "═" * 46
        return f"{separator}\n{header}\n{separator}\n{content}"

    @staticmethod
    def _read_raw_checked(path: Path) -> Tuple[str, bool]:
        """Read a memory file's raw text, distinguishing unreadable from empty.

        Returns ``(raw, read_ok)``. ``read_ok`` is False ONLY when the file
        EXISTS but could not be read — an absent file is a clean ``("", True)``.
        Invalid UTF-8 counts as unreadable too: the bytes on disk hold content
        we cannot faithfully round-trip, so a rewrite would corrupt or discard
        it just like a failed read. Read-modify-write callers must treat
        ``read_ok=False`` as "abort" rather than "empty store", or a transient
        read failure would let them persist over — and wipe — the on-disk
        memory (issue #26045 is about the same class: never rewrite a file
        from a view that isn't the real one).

        No file locking needed: _write_file uses atomic rename, so readers
        always see either the previous complete file or the new complete file.
        """
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
        except (OSError, IOError, UnicodeDecodeError):
            return "", False

    @staticmethod
    def _parse_entries(raw: str) -> List[str]:
        """Split raw memory-file text into stripped, non-empty entries."""
        if not raw.strip():
            return []
        # Use ENTRY_DELIMITER for consistency with _write_file. Splitting by "§"
        # alone would incorrectly split entries that contain "§" in their content.
        entries = [e.strip() for e in raw.split(ENTRY_DELIMITER)]
        return [e for e in entries if e]

    @staticmethod
    def _read_entries_checked(path: Path) -> Tuple[List[str], bool]:
        """Read + parse a memory file, distinguishing unreadable from empty.

        Returns ``(entries, read_ok)`` — see ``_read_raw_checked`` for the
        ``read_ok`` contract.
        """
        raw, read_ok = MemoryStore._read_raw_checked(path)
        if not read_ok:
            return [], False
        return MemoryStore._parse_entries(raw), True

    @staticmethod
    def _read_file(path: Path) -> List[str]:
        """Read a memory file and split into entries (empty list on any error).

        Retained for read-only callers (``load_from_disk``) that build in-memory
        state without persisting; a failed read degrading to ``[]`` there is
        harmless because nothing is written back. Read-modify-write paths use
        ``_read_raw_checked`` so they can refuse to overwrite an unreadable
        file — see ``_reload_target``.
        """
        return MemoryStore._read_entries_checked(path)[0]

    def _detect_external_drift(self, target: str, raw: str) -> Optional[str]:
        """Return a backup-path string if on-disk content shows external drift.

        *raw* is the file content already read by the caller's checked read
        (``_read_raw_checked``). Drift detection MUST operate on that same
        snapshot — an earlier version re-read the file here and treated a
        failed second read as "no drift", which let a mutation proceed from a
        stale first snapshot and rewrite away content an external writer added
        between the two reads.

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
            atomic_write_text(path, content, tmp_prefix=".mem_")
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


