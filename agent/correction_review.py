"""Shared correction-detection + review-spawn decision for turn finalizers.

Both the default finalizer (``agent/turn_finalizer.py``) and the Codex-runtime
finalizer (``agent/codex_runtime.py``) route through ``decide_correction_review``
so the learn-from-corrections behavior cannot drift between the two runtimes.
Before this seam existed the Codex path carried an unmodified nudge-only gate and
silently never detected or recorded a user correction (defect: codex parity).

The decision has three moving parts, all derived deterministically:

* DETECT + RECORD (always, when a correction is present). ``detect_correction``
  classifies the turn (INTERRUPT / DENY / STEER); the per-agent
  ``_record_turn_correction`` hook feeds the recurrence tracker and returns the
  promoted tier. This is the single durable gate for an unpromoted correction
  and runs whether or not the expensive LLM review fork is spawned — including
  the loud interrupted/denied turns the legacy ``not interrupted`` gate dropped.

* SPAWN the LLM review fork ONLY when a nudge independently fired
  (``_healthy_review`` — the legacy healthy-completion path) OR the correction
  was promoted to DURABLE (cross-session recurrence — the sole Phase-1 durable
  trigger; explicit-remember wiring is deferred). A pure-transient
  correction with no nudge is already recorded deterministically and the fork
  would be write-blocked anyway, so spawning it would burn an aux-model call for
  nothing (defect: wasted aux-model spend on pure-transient corrections).

* BLOCK durable writes on the fork (X1) whenever a correction is present and NOT
  yet durable — UNIVERSALLY, even when a nudge co-occurs. The co-occurring
  nudge's own durable write is deferred to the next nudge interval (the accepted
  safety trade) so a transient correction can never ride a nudge into a durable
  write. The deterministic recurrence guard stays the sole durable gate for an
  unpromoted correction.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional


def detect_and_record_correction(
    agent,
    *,
    messages: List[Dict],
    interrupted: bool,
    interrupt_message: Optional[str],
    turn_exit_reason: Optional[str],
) -> Optional[Dict[str, Any]]:
    """Deterministically detect a structured correction and record it.

    Returns the correction hint (with the recorder's promoted ``tier`` /
    ``durable`` threaded back in) or ``None`` when the turn is not a learnable
    correction. Best-effort: never raises into turn finalization.
    """
    try:
        from agent.correction_learning import detect_correction

        correction = detect_correction(
            messages,
            interrupted=interrupted,
            interrupt_message=interrupt_message,
            turn_exit_reason=turn_exit_reason,
            session_id=getattr(agent, "session_id", "") or "",
        )
        if correction is None:
            return None
        hint: Dict[str, Any] = {
            "kind": correction.kind,
            "signature": correction.signature,
            "context": correction.context,
            "target": correction.target,
            # Transient until the recurrence guard says otherwise. This is the
            # safe default: the LLM reviewer is never told to durably persist a
            # correction we have not confirmed is durable.
            "tier": "transient",
            "durable": False,
        }
        # Feed the recurrence tracker (signature -> distinct sessions). Transient
        # by default; promotes to durable only on cross-session recurrence (the
        # sole Phase-1 durable trigger; explicit-remember is deferred and not
        # wired). Fail-open via the agent hook. The returned tier is
        # threaded back into the hint so the review prompt stays tier-aware.
        recorder = getattr(agent, "_record_turn_correction", None)
        if callable(recorder):
            try:
                outcome = recorder(hint)
                if isinstance(outcome, dict):
                    hint["tier"] = outcome.get("tier", "transient")
                    hint["durable"] = bool(outcome.get("durable"))
            except Exception:
                pass
        return hint
    except Exception:
        # Detection is best-effort; never let it break turn finalization.
        return None


def decide_correction_review(
    agent,
    *,
    final_text: Optional[str],
    interrupted: bool,
    messages: List[Dict],
    interrupt_message: Optional[str],
    turn_exit_reason: Optional[str],
    should_review_memory: bool,
    should_review_skills: bool,
) -> Dict[str, Any]:
    """Detect+record a correction and decide the background review fork.

    Returns a decision dict::

        {
          "spawn": bool,                 # spawn the LLM review fork
          "review_memory": bool,         # pass-through for the fork
          "review_skills": bool,         # pass-through for the fork
          "correction_hint": dict|None,  # the detected correction
          "block_durable_writes": bool,  # strip the fork's durable writers (X1)
        }

    See the module docstring for the spawn and block rules. Detection +
    recording always runs (deterministic) even when ``spawn`` is False.
    """
    # Legacy healthy-completion nudge path: a counter fired AND the turn
    # completed normally. Preserved exactly.
    healthy_review = bool(
        final_text
        and not interrupted
        and (should_review_memory or should_review_skills)
    )

    correction_hint = detect_and_record_correction(
        agent,
        messages=messages,
        interrupted=interrupted,
        interrupt_message=interrupt_message,
        turn_exit_reason=turn_exit_reason,
    )
    correction_present = correction_hint is not None
    correction_durable = bool(
        correction_present and correction_hint.get("durable")
    )

    # X1 (universal): any unpromoted correction strips the fork's durable
    # writers — even when a nudge co-occurs. The deterministic CorrectionLearner
    # is the single durable gate for an unpromoted correction.
    block_durable_writes = correction_present and not correction_durable

    # Spawn only when a nudge fired OR the correction is already durable. A
    # pure-transient correction with no nudge is already recorded
    # deterministically; the fork would be write-blocked, so spawning it would
    # waste an aux-model call.
    spawn = bool(healthy_review or correction_durable)

    return {
        "spawn": spawn,
        # Mirror the legacy finalizer: a present correction implies a memory
        # review focus so the fork (when it spawns) captures it.
        "review_memory": bool(should_review_memory or correction_present),
        "review_skills": bool(should_review_skills),
        "correction_hint": correction_hint,
        "block_durable_writes": bool(block_durable_writes),
    }


__all__ = ["decide_correction_review", "detect_and_record_correction"]
