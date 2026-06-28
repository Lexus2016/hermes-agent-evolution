"""Learn from user corrections — lean Phase 1 (per-user Fast Loop).

Principle: a real user correcting a real agent on a real task is the
highest-signal feedback an agent gets. Today Hermes captures *some* of it
(the post-turn LLM ``background_review`` writes preferences to per-profile
memory/skills) but misses the loudest, most structured signals — interrupted
and denied turns are skipped entirely (``agent/turn_finalizer.py``'s
``not interrupted`` guard). This module adds the smallest end-to-end slice
that closes that gap *safely*.

What this module is (Phase 1, deliberately minimal):

1. ``detect_correction`` — DETERMINISTIC detection of a structured correction
   on a completed turn. Three kinds, all from runtime markers (no fuzzy text
   regex, no LLM):
     * ``INTERRUPT`` — the user stopped the agent mid-turn AND supplied a
       redirect message (``agent._interrupt_message``). Runtime scope: this is
       live on the default runtime (the finalizer captures the message before
       clearing it); on the codex runtime INTERRUPT stays inert because that
       runtime does not propagate user interrupts into its session (a
       pre-existing platform gap, deferred). DENY/STEER work on both runtimes.
     * ``DENY`` — a tool result carried the explicit ``user_denied`` marker
       (a real user vetoed the action at the approval prompt). Automatic
       safety/validation blocks (which also set ``status: "blocked"`` but
       carry no user denial) are deliberately excluded.
     * ``STEER`` — an out-of-band user message was injected mid-turn
       (``STEER_MARKER_OPEN`` in a tool result).

2. ``CorrectionLearner`` — the GENERALIZATION GUARD. A correction captured
   from these signals is TRANSIENT by default. In Phase 1 it is promoted to
   DURABLE (written to the persistent memory store that re-injects into future
   sessions) on a SINGLE production trigger:
     (a) the same correction *signature* recurs across >= 2 DISTINCT
         sessions (cross-session recurrence).
   Cross-session recurrence is therefore the SOLE production durable trigger in
   Phase 1.

   ``record(remember=True)`` also forces a durable promotion, but that path is
   NOT WIRED to any production signal in Phase 1: no caller threads an explicit
   user "remember this" through it — ``run_agent.py`` calls ``record(rec)`` with
   ``remember`` defaulting False, and ``correction_review`` never derives a
   remember flag. The fuzzy "remember this" detector that would feed it is
   DEFERRED to a later phase. The ``remember`` parameter is retained ONLY as the
   tested seam that future path will use; treat explicit-remember as
   not-yet-reachable in production.

   Transient items live in a lightweight local JSON store and never change
   behavior. The recurrence tracker (signature -> distinct sessions) is the
   load-bearing safety piece: it is the difference between "the agent learned
   a stable preference" and "the agent over-fit one user's one-off whim".

3. PROVENANCE + UNLEARN — every durable item is tagged with its origin
   (signal kind, session, signature, timestamps, promotion reason) in a
   ledger. ``unlearn(provenance_id)`` removes the durable item from both the
   ledger and the memory store, so it stops injecting. Reversible by
   construction.

What this module is NOT (deferred to later phases): the multi-dimensional
evidence vector, the fleet/global consensus path, calibration / positive-
negative controls, the adversarial counter-reviewer, TTL / model-version
tagging, config-over-prompt routing. See
``.plans/learn-from-user-corrections-SPEC.md`` §13 for the phasing.

The store is fail-open: a broken or unwritable state file must never crash a
user's turn. Every disk touch is guarded; on failure the learner degrades to
"transient only" rather than raising.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from agent.prompt_builder import STEER_MARKER_CLOSE, STEER_MARKER_OPEN

logger = logging.getLogger(__name__)

# Memory target the durable rule is written to. MEMORY.md is the per-profile
# store that ``MemoryStore.load_from_disk`` snapshots into the system prompt at
# the start of every future session (see ``tools/memory_tool.py`` /
# ``agent/system_prompt.py``). Writing here is what makes a learned correction
# re-enter behavior next session.
DURABLE_MEMORY_TARGET = "memory"

# Evidence threshold: how many DISTINCT sessions must show the same signature
# before a transient correction is promoted to durable. 2 = "seen again in a
# new session" — the minimum that distinguishes a stable pattern from a one-off.
RECURRENCE_THRESHOLD = 2


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------


@dataclass
class CorrectionRecord:
    """A structured correction detected on a completed turn.

    Deliberately small: kind, a stable signature (what the recurrence tracker
    keys on), the minimal human-readable context, the session it came from,
    and a timestamp. No raw transcript, no scoring vector.
    """

    kind: str  # "INTERRUPT" | "DENY" | "STEER"
    signature: str
    context: str
    session_id: str
    ts: str
    target: Optional[str] = None  # tool/skill name when known
    metadata: Dict[str, Any] = field(default_factory=dict)


def _now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


def _normalize(text: str) -> str:
    """Lowercase + collapse whitespace for a stable, comparable signature."""
    return " ".join((text or "").lower().split())


def _signature(kind: str, key_text: str, target: Optional[str]) -> str:
    """Deterministic short signature for recurrence matching.

    Same correction (kind + normalized salient text + target) -> same
    signature across sessions and processes. A truncated SHA-256 keeps it
    compact and non-reversible (no raw text in the key itself).
    """
    basis = f"{kind}\x1f{target or ''}\x1f{_normalize(key_text)}"
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:16]


def _iter_tool_messages(messages: List[Dict]):
    for m in messages or []:
        if isinstance(m, dict) and m.get("role") == "tool":
            yield m


def _tool_text(m: Dict) -> str:
    c = m.get("content")
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        return " ".join(
            b.get("text", "") for b in c if isinstance(b, dict)
        )
    return ""


def _detect_deny(messages: List[Dict]) -> Optional[Dict[str, Any]]:
    """Return {target, error} for the LAST genuine USER-denied tool result.

    A DENY correction must reflect a real user veto — NOT an automatic safety
    or validation block. Many automatic blocks (the dangerous-command guard at
    ``tools/terminal_tool.py`` and the workdir shell-injection validator) also
    emit ``status: "blocked"`` with no user involvement, so keying on
    ``status`` alone would mint false corrections from recurring automatic
    blocks (defect X2).

    The discriminator is the explicit ``user_denied`` marker that the approval
    flow stamps onto the tool result ONLY when a user actively denied the action
    at the approval prompt (``tools/approval.py`` -> ``tools/terminal_tool.py``).
    Timeouts and automatic blocks never carry it. Parse defensively; non-JSON
    tool output is ignored.
    """
    found = None
    for m in _iter_tool_messages(messages):
        text = _tool_text(m)
        if not text:
            continue
        try:
            data = json.loads(text)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(data, dict) and data.get("user_denied") is True:
            found = {
                "target": m.get("name"),
                "error": str(data.get("error", "")),
            }
    return found


def _detect_steer(messages: List[Dict]) -> Optional[Dict[str, Any]]:
    """Return {target, text} for the LAST mid-turn steer, else None."""
    found = None
    for m in _iter_tool_messages(messages):
        text = _tool_text(m)
        if STEER_MARKER_OPEN in text:
            start = text.index(STEER_MARKER_OPEN) + len(STEER_MARKER_OPEN)
            end = text.find(STEER_MARKER_CLOSE, start)
            steer_text = text[start:end] if end != -1 else text[start:]
            found = {
                "target": m.get("name"),
                "text": steer_text.strip(),
            }
    return found


def detect_correction(
    messages: List[Dict],
    *,
    interrupted: bool,
    interrupt_message: Optional[str],
    turn_exit_reason: Optional[str],
    session_id: str,
    ts: Optional[str] = None,
) -> Optional[CorrectionRecord]:
    """Deterministically classify a completed turn as a structured correction.

    Returns a ``CorrectionRecord`` for the single most salient structured
    correction on the turn, or ``None`` if the turn is not a learnable
    correction.

    Precedence (highest-signal first): INTERRUPT (the user actively stopped
    the agent and said what to do instead) > DENY (a vetoed action) > STEER
    (a mid-turn redirect). A turn can carry more than one; we capture the
    loudest. No fuzzy text matching — every branch keys off a runtime marker.

    A plain interrupt with NO redirect message is NOT a correction we can
    learn from (there is nothing to capture); it returns ``None`` so the
    caller preserves existing plain-interrupt behavior.
    """
    ts = ts or _now_iso()

    # INTERRUPT — only learnable when the user supplied redirect text.
    if interrupted and interrupt_message and interrupt_message.strip():
        msg = interrupt_message.strip()
        return CorrectionRecord(
            kind="INTERRUPT",
            signature=_signature("INTERRUPT", msg, None),
            context=msg,
            session_id=session_id,
            ts=ts,
            metadata={"turn_exit_reason": turn_exit_reason},
        )

    # DENY — a tool was blocked/vetoed.
    deny = _detect_deny(messages)
    if deny is not None:
        err = deny["error"] or "command denied"
        return CorrectionRecord(
            kind="DENY",
            signature=_signature("DENY", err, deny["target"]),
            context=err,
            session_id=session_id,
            ts=ts,
            target=deny["target"],
        )

    # STEER — a mid-turn out-of-band user redirect.
    steer = _detect_steer(messages)
    if steer is not None and steer["text"]:
        return CorrectionRecord(
            kind="STEER",
            signature=_signature("STEER", steer["text"], steer["target"]),
            context=steer["text"],
            session_id=session_id,
            ts=ts,
            target=steer["target"],
        )

    return None


# ---------------------------------------------------------------------------
# Generalization guard + store
# ---------------------------------------------------------------------------


def _default_store_dir() -> Path:
    """Per-profile correction-learning directory.

    Resolves under the same profile-scoped Hermes home that the memory store
    uses, so corrections live next to the data they may eventually promote.
    """
    from hermes_constants import get_hermes_home

    return get_hermes_home() / "corrections"


class CorrectionLearner:
    """Owns the recurrence tracker, transient store, and durable ledger.

    Files under ``store_dir``:
      * ``recurrence.json`` — ``{signature: {"sessions": [...], "kind": ...}}``
        the distinct-session counter that flips transient -> durable.
      * ``transient.json`` — list of transient correction records (audit /
        future use; does NOT change behavior).
      * ``learned.json`` — the durable provenance ledger.

    ``memory_sink`` is the durable write target — in production a
    ``MemoryStore`` (writes MEMORY.md, the re-injection path). It must expose
    ``add(target, content, **kw)`` and ``remove(target, content_substr, **kw)``.
    Injected for testability and to keep this module free of a hard import on
    the memory subsystem.
    """

    def __init__(self, store_dir: Optional[Path] = None, memory_sink: Any = None):
        self.store_dir = Path(store_dir) if store_dir else _default_store_dir()
        self.memory_sink = memory_sink
        self._recurrence_path = self.store_dir / "recurrence.json"
        self._transient_path = self.store_dir / "transient.json"
        self._learned_path = self.store_dir / "learned.json"

    # -- fail-open JSON helpers (mirrors scripts/evolution_* pattern) -------

    def _read_json(self, path: Path, default):
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return default

    def _write_json(self, path: Path, payload) -> None:
        # Best-effort; the caller wraps this so a raise never reaches a turn.
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(
            json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8"
        )
        os.replace(tmp, path)

    # -- public API ---------------------------------------------------------

    def record(
        self, rec: CorrectionRecord, *, remember: bool = False
    ) -> Dict[str, Any]:
        """Register a detected correction and apply the generalization guard.

        Returns ``{"tier", "durable", "provenance_id", "sightings", "reason"}``.
        Fail-open: any persistence error degrades to a transient result rather
        than raising.

        ``remember``: force a durable promotion. NOTE — in Phase 1 this is NOT
        wired to any production caller (nothing sets it True; ``run_agent.py``
        calls ``record(rec)``). Cross-session recurrence is the sole production
        durable trigger; explicit-remember wiring is deferred to a later phase.
        The parameter is kept as the tested seam that path will use.
        """
        try:
            return self._record_inner(rec, remember=remember)
        except Exception as e:  # pragma: no cover - defensive, fail-open
            logger.warning("correction record failed (fail-open): %s", e)
            return {
                "tier": "transient",
                "durable": False,
                "provenance_id": None,
                "sightings": 0,
                "reason": "error",
            }

    def _record_inner(
        self, rec: CorrectionRecord, *, remember: bool
    ) -> Dict[str, Any]:
        # 1. Update the recurrence tracker (distinct sessions per signature).
        recurrence = self._read_json(self._recurrence_path, {})
        slot = recurrence.get(rec.signature) or {"sessions": [], "kind": rec.kind}
        sessions = slot.get("sessions", [])
        if rec.session_id not in sessions:
            sessions.append(rec.session_id)
        slot["sessions"] = sessions
        slot["kind"] = rec.kind
        recurrence[rec.signature] = slot
        self._write_json(self._recurrence_path, recurrence)

        sightings = len(sessions)

        # 2a. Idempotent promotion. If this signature is ALREADY durable,
        #     return the existing provenance without re-writing memory or
        #     appending a duplicate ledger entry (otherwise every later
        #     sighting bloats the ledger and re-writes MEMORY.md). A learned
        #     rule is a single object, not one-per-sighting.
        for entry in self.list_durable():
            if entry.get("signature") == rec.signature:
                return {
                    "tier": "durable",
                    "durable": True,
                    "provenance_id": entry.get("provenance_id"),
                    "sightings": sightings,
                    "reason": "already_durable",
                }

        # 2b. Decide tier. Cross-session recurrence is the sole PRODUCTION
        #     durable trigger in Phase 1. The ``remember`` fast-path also
        #     promotes durably but is not wired to any production caller yet
        #     (deferred — see class/module docstring); it stays here only as the
        #     tested seam. Otherwise transient.
        if remember:
            reason = "explicit_remember"
            durable = True
        elif sightings >= RECURRENCE_THRESHOLD:
            reason = "recurrence"
            durable = True
        else:
            reason = "first_sighting"
            durable = False

        if not durable:
            self._append_transient(rec, sightings)
            return {
                "tier": "transient",
                "durable": False,
                "provenance_id": None,
                "sightings": sightings,
                "reason": reason,
            }

        # 3. Promote to durable: write to the memory sink (re-injection path)
        #    and record provenance in the ledger.
        provenance_id = self._promote(rec, reason=reason, sightings=sightings)
        return {
            "tier": "durable",
            "durable": True,
            "provenance_id": provenance_id,
            "sightings": sightings,
            "reason": reason,
        }

    def _append_transient(self, rec: CorrectionRecord, sightings: int) -> None:
        items = self._read_json(self._transient_path, [])
        items.append({
            "kind": rec.kind,
            "signature": rec.signature,
            "context": rec.context,
            "session_id": rec.session_id,
            "ts": rec.ts,
            "sightings": sightings,
        })
        # Bound growth — keep the most recent 500 transient records.
        if len(items) > 500:
            items = items[-500:]
        self._write_json(self._transient_path, items)

    def _durable_text(self, rec: CorrectionRecord) -> str:
        """The behavior-changing sentence written to MEMORY.md.

        Phrased as a learned user preference/correction so the next session's
        agent reads it as guidance.
        """
        label = {
            "INTERRUPT": "The user redirected a turn",
            "DENY": "The user denied an action",
            "STEER": "The user steered mid-turn",
        }.get(rec.kind, "The user corrected the agent")
        return f"[learned correction] {label}: {rec.context}".strip()

    def _promote(
        self, rec: CorrectionRecord, *, reason: str, sightings: int
    ) -> str:
        provenance_id = uuid.uuid4().hex[:12]
        content = self._durable_text(rec)

        # Atomicity ordering. The ledger write and the durable memory write are
        # NOT transactional, so a failure between them must fail SAFE. Write the
        # LEDGER entry FIRST, then the memory line: a crash after the ledger
        # write but before/within the memory write leaves a ledger entry with no
        # memory line — visible (``injected: False``), cleanable, and crucially
        # still UNLEARNABLE. The reverse order (memory-first) would orphan a
        # MEMORY.md line with no ledger entry: it would re-inject into every
        # future session with no provenance id to ``unlearn`` it.
        entry = {
            "provenance_id": provenance_id,
            "origin_kind": rec.kind,
            "signature": rec.signature,
            "session_id": rec.session_id,
            "context": rec.context,
            "content": content,
            "target": rec.target,
            "tier": "durable",
            "reason": reason,
            "sightings": sightings,
            "ts": rec.ts,
            "promoted_ts": _now_iso(),
            "injected": False,
        }
        ledger = self._read_json(self._learned_path, [])
        ledger.append(entry)
        self._write_json(self._learned_path, ledger)

        # Now write to the durable re-injection path. Best-effort: if the sink
        # write fails the ledger entry remains (so unlearn stays coherent),
        # simply marked ``injected: False``.
        injected = False
        if self.memory_sink is not None:
            try:
                result = self.memory_sink.add(
                    DURABLE_MEMORY_TARGET, content
                )
                injected = bool(
                    result.get("success", True)
                    if isinstance(result, dict) else result
                )
            except Exception as e:
                logger.warning("durable memory write failed: %s", e)

        # Reflect the injection outcome back into the ledger. Re-read first so a
        # concurrent writer is not clobbered, then patch this entry in place.
        # Guarded: a failure here must not undo a successful memory injection.
        if injected:
            try:
                ledger = self._read_json(self._learned_path, [])
                for e in ledger:
                    if e.get("provenance_id") == provenance_id:
                        e["injected"] = True
                        break
                self._write_json(self._learned_path, ledger)
            except Exception as e:
                logger.warning("ledger injected-flag update failed: %s", e)

        return provenance_id

    # -- ledger queries -----------------------------------------------------

    def list_durable(self) -> List[Dict[str, Any]]:
        return self._read_json(self._learned_path, [])

    def get_durable(self, provenance_id: str) -> Optional[Dict[str, Any]]:
        for e in self.list_durable():
            if e.get("provenance_id") == provenance_id:
                return e
        return None

    # -- unlearn (symmetric, reversible) -----------------------------------

    def unlearn(self, provenance_id: str) -> bool:
        """Remove a durable learned item by its provenance id.

        Removes it from the memory sink (so it stops injecting) and from the
        ledger. Returns True if an item was removed, False if the id was
        unknown. Fail-open on persistence errors.
        """
        try:
            ledger = self.list_durable()
            entry = next(
                (e for e in ledger if e.get("provenance_id") == provenance_id),
                None,
            )
            if entry is None:
                return False
            if self.memory_sink is not None:
                try:
                    self.memory_sink.remove(
                        DURABLE_MEMORY_TARGET, entry.get("content", "")
                    )
                except Exception as e:
                    logger.warning("unlearn memory remove failed: %s", e)
            remaining = [
                e for e in ledger if e.get("provenance_id") != provenance_id
            ]
            self._write_json(self._learned_path, remaining)
            # Reset the recurrence evidence for this signature so the user's
            # "unlearn" is not silently undone by the very next sighting (which
            # would otherwise still see >= threshold distinct sessions and
            # re-promote instantly). The correction must re-accumulate fresh
            # cross-session evidence to become durable again.
            signature = entry.get("signature")
            if signature:
                try:
                    recurrence = self._read_json(self._recurrence_path, {})
                    if signature in recurrence:
                        del recurrence[signature]
                        self._write_json(self._recurrence_path, recurrence)
                except Exception as e:
                    logger.warning("unlearn recurrence reset failed: %s", e)
            return True
        except Exception as e:  # pragma: no cover - defensive, fail-open
            logger.warning("unlearn failed (fail-open): %s", e)
            return False


__all__ = [
    "CorrectionRecord",
    "CorrectionLearner",
    "detect_correction",
    "RECURRENCE_THRESHOLD",
    "DURABLE_MEMORY_TARGET",
]
