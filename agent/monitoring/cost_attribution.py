"""Per-action cost attribution + cost-anomaly detection for monitoring.

Content-free (numeric fields + anomaly flags only), fail-closed (never raises).
"""

from __future__ import annotations

import threading
import time
from typing import Any, Dict, Optional

_COST_FIELDS = {
    "prompt_tokens": "hermes.prompt_tokens",
    "completion_tokens": "hermes.completion_tokens",
    "total_tokens": "hermes.total_tokens",
    "duration_ms": "hermes.duration_ms",
    "cost_usd": "hermes.cost_usd",
}
_WINDOW_S, _REDUNDANT, _RETRY = 300.0, 4, 4


def cost_attributes(event: Dict[str, Any]) -> Dict[str, Any]:
    """Map numeric usage fields onto ``hermes.*`` span attributes."""
    out: Dict[str, Any] = {}
    for key, name in _COST_FIELDS.items():
        v = event.get(key)
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            out[name] = v
    return out


def anomaly_attributes(signal: Dict[str, Any]) -> Dict[str, Any]:
    """Map a cost-anomaly signal onto ``hermes.*`` span attributes."""
    if not signal or not signal.get("anomaly"):
        return {}
    attrs: Dict[str, Any] = {"hermes.cost_anomaly": signal["anomaly"]}
    if signal.get("tool"):
        attrs["hermes.anomaly_tool"] = signal["tool"]
    if signal.get("count"):
        attrs["hermes.anomaly_count"] = signal["count"]
    return attrs


class CostAnomalyDetector:
    """Bounded, thread-safe detector for redundant calls and retry loops."""

    def __init__(
        self,
        *,
        redundant_threshold: int = _REDUNDANT,
        retry_threshold: int = _RETRY,
        window_s: float = _WINDOW_S,
    ) -> None:
        self._redundant = redundant_threshold
        self._retry = retry_threshold
        self._window_s = window_s
        self._recent: Dict[tuple, list] = {}
        self._lock = threading.Lock()

    def detect(
        self,
        tool: str,
        session_id: str = "",
        status: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Record a tool call and return a cost-anomaly signal (or {})."""
        try:
            now = time.monotonic()
            is_error = status == "error"
            key = (session_id or "", tool or "")
            cutoff = now - self._window_s
            with self._lock:
                seq = [t for t in self._recent.get(key, []) if t[0] >= cutoff]
                seq.append((now, is_error))
                self._recent[key] = seq
                errors = sum(1 for _, e in seq if e)
                successes = len(seq) - errors
                if is_error and errors >= self._retry:
                    self._recent.pop(key, None)
                    return {"anomaly": "retry_loop", "count": errors}
                if not is_error and successes >= self._redundant:
                    self._recent.pop(key, None)
                    return {"anomaly": "redundant_calls", "count": successes}
        except Exception:
            return {}
        return {}


_DETECTOR = CostAnomalyDetector()


def detect_cost_anomaly(
    event: Dict[str, Any],
    detector: Optional[CostAnomalyDetector] = None,
) -> Dict[str, Any]:
    """Feed a tool-call event to a detector and return its anomaly signal."""
    try:
        det = detector if detector is not None else _DETECTOR
        return det.detect(
            event.get("tool") or "",
            event.get("session_id") or "",
            event.get("status"),
        )
    except Exception:
        return {}
