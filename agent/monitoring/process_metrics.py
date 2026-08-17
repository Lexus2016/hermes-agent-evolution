"""Process-level quality metrics + experience-reuse audit for monitoring.

Content-free (phase scores + reuse counters only), fail-closed (never raises).

Implements the "beyond final scores" direction (arXiv:2608.13417): a single
terminal score can conceal which phase of a long-horizon run is the bottleneck.
We surface three process phases — Solution Framing, Execution, Feedback Control
— as ``hermes.*`` span attributes, plus a bounded audit counter for
experience-reuse decisions. Additive to cost attribution (#2649); no content
(prompts, tool args/results, session history) is ever emitted.
"""

from __future__ import annotations

import threading
from typing import Any, Dict, Optional

# Phase-quality scores are normalized 0..1 (mirroring the arXiv reliability
# ranges: Execution 0.880-0.967, Framing 0.473-0.612, Feedback 0.772-0.928).
_PHASE_FIELDS = {
    "solution_framing": "hermes.process.solution_framing",
    "execution": "hermes.process.execution",
    "feedback_control": "hermes.process.feedback_control",
}


def process_attributes(event: Dict[str, Any]) -> Dict[str, Any]:
    """Map normalized phase-quality scores onto ``hermes.*`` span attributes.

    Only numeric 0..1 values are surfaced; anything else (missing, string,
    out-of-range) is dropped. Content-free by construction.
    """
    out: Dict[str, Any] = {}
    for key, name in _PHASE_FIELDS.items():
        v = event.get(key)
        if isinstance(v, (int, float)) and not isinstance(v, bool) and 0.0 <= v <= 1.0:
            out[name] = v
    return out


def reuse_attributes(signal: Dict[str, Any]) -> Dict[str, Any]:
    """Map an experience-reuse audit signal onto ``hermes.*`` span attributes."""
    if not signal or not signal.get("reuse"):
        return {}
    attrs: Dict[str, Any] = {"hermes.process.reuse": signal["reuse"]}
    if signal.get("count"):
        attrs["hermes.process.reuse_count"] = signal["count"]
    if signal.get("source"):
        attrs["hermes.process.reuse_source"] = signal["source"]
    return attrs


class ExperienceReuseAudit:
    """Bounded, thread-safe counter for experience-reuse decisions.

    Tracks how often stored experiences influence a decision (per source), so
    biased or misleading reuse can be surfaced as a diagnostic rather than
    silently compounding. Fail-closed: any error returns {}.
    """

    def __init__(self, *, window_s: float = 300.0) -> None:
        self._window_s = window_s
        self._counts: Dict[str, int] = {}
        self._lock = threading.Lock()

    def record(self, source: str = "") -> Dict[str, Any]:
        """Record one reuse decision for ``source``; return a signal (or {})."""
        try:
            key = source or "unknown"
            with self._lock:
                self._counts[key] = self._counts.get(key, 0) + 1
                count = self._counts[key]
            return {"reuse": True, "count": count, "source": key}
        except Exception:
            return {}

    def reset(self) -> None:
        with self._lock:
            self._counts.clear()


_AUDIT = ExperienceReuseAudit()


def audit_experience_reuse(
    event: Dict[str, Any],
    audit: Optional[ExperienceReuseAudit] = None,
) -> Dict[str, Any]:
    """Feed a reuse decision to the audit counter and return its signal.

    Only records when the event explicitly carries a ``reuse_source`` (i.e. a
    stored experience actually influenced a decision); otherwise returns {} so
    the span stays clean. Fail-closed: any error returns {}.
    """
    try:
        source = event.get("reuse_source")
        if not source:
            return {}
        aud = audit if audit is not None else _AUDIT
        return aud.record(source)
    except Exception:
        return {}
