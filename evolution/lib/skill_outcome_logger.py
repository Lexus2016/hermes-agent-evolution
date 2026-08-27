# -*- coding: utf-8 -*-
"""Triggered-skill outcome logging and auto-demotion (#3218, child of #3210).

Closes the feedback loop for triggered skills:
- Logs (skill_id, trigger_confidence, success, revision) per triggered use.
- Tracks triggered_use_count and triggered_success_count.
- Demotes skills whose triggered success rate falls below 50% over >= 5 uses.
- Supports re-promotion and stats inspection.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

DEFAULT_MIN_USES = 5
DEFAULT_MIN_SUCCESS_RATE = 0.5


class _SkillOutcomeStore:
    """Thread-safe in-memory and persistent store for skill execution outcomes."""

    def __init__(self, storage_file: Optional[Path] = None) -> None:
        self._lock = threading.Lock()
        self._storage_file = storage_file
        self._stats: Dict[str, Dict[str, Any]] = {}
        self._events: List[Dict[str, Any]] = []
        if storage_file and storage_file.is_file():
            self._load()

    def _load(self) -> None:
        try:
            if self._storage_file and self._storage_file.is_file():
                data = json.loads(self._storage_file.read_text(encoding="utf-8"))
                self._stats = data.get("stats", {})
                self._events = data.get("events", [])
        except Exception:
            pass

    def _save(self) -> None:
        if not self._storage_file:
            return
        try:
            self._storage_file.parent.mkdir(parents=True, exist_ok=True)
            self._storage_file.write_text(
                json.dumps({"stats": self._stats, "events": self._events[-500:]}, indent=2),
                encoding="utf-8",
            )
        except Exception:
            pass

    def record_outcome(
        self,
        skill_id: str,
        trigger_confidence: float,
        success: bool,
        revision: str = "",
    ) -> Dict[str, Any]:
        with self._lock:
            sid = str(skill_id).strip()
            if not sid:
                return {}
            rec = self._stats.setdefault(
                sid,
                {
                    "skill_id": sid,
                    "triggered_use_count": 0,
                    "triggered_success_count": 0,
                    "demoted": False,
                    "revision": revision,
                },
            )
            rec["triggered_use_count"] += 1
            if success:
                rec["triggered_success_count"] += 1
            if revision:
                rec["revision"] = revision

            uses = rec["triggered_use_count"]
            succ = rec["triggered_success_count"]
            rate = succ / uses if uses > 0 else 0.0
            rec["success_rate"] = rate

            # Demotion rule: rate < 0.5 over >= 5 uses
            if uses >= DEFAULT_MIN_USES and rate < DEFAULT_MIN_SUCCESS_RATE:
                rec["demoted"] = True
            elif uses >= DEFAULT_MIN_USES and rate >= DEFAULT_MIN_SUCCESS_RATE:
                rec["demoted"] = False

            self._events.append({
                "skill_id": sid,
                "trigger_confidence": float(trigger_confidence),
                "success": bool(success),
                "revision": revision,
            })
            self._save()
            return dict(rec)

    def is_demoted(
        self,
        skill_id: str,
        min_uses: int = DEFAULT_MIN_USES,
        min_success_rate: float = DEFAULT_MIN_SUCCESS_RATE,
    ) -> bool:
        with self._lock:
            sid = str(skill_id).strip()
            rec = self._stats.get(sid)
            if not rec:
                return False
            uses = rec.get("triggered_use_count", 0)
            succ = rec.get("triggered_success_count", 0)
            if uses < min_uses:
                return False
            rate = succ / uses if uses > 0 else 0.0
            return rate < min_success_rate or bool(rec.get("demoted", False))

    def get_stats(self, skill_id: Optional[str] = None) -> Dict[str, Any]:
        with self._lock:
            if skill_id:
                sid = str(skill_id).strip()
                return dict(self._stats.get(sid, {}))
            return {k: dict(v) for k, v in self._stats.items()}

    def promote_skill(self, skill_id: str) -> Dict[str, Any]:
        """Re-promote a skill, clearing its demotion state."""
        with self._lock:
            sid = str(skill_id).strip()
            rec = self._stats.setdefault(
                sid,
                {
                    "skill_id": sid,
                    "triggered_use_count": 0,
                    "triggered_success_count": 0,
                    "demoted": False,
                },
            )
            rec["demoted"] = False
            rec["triggered_use_count"] = 0
            rec["triggered_success_count"] = 0
            rec["success_rate"] = 1.0
            self._save()
            return dict(rec)


_GLOBAL_STORE = _SkillOutcomeStore()


def record_skill_outcome(
    skill_id: str,
    trigger_confidence: float,
    success: bool,
    revision: str = "",
) -> Dict[str, Any]:
    """Record execution outcome for a triggered skill."""
    return _GLOBAL_STORE.record_outcome(
        skill_id=skill_id,
        trigger_confidence=trigger_confidence,
        success=success,
        revision=revision,
    )


def is_skill_demoted(skill_id: str) -> bool:
    """Return True if skill has been demoted due to poor triggered success rate."""
    return _GLOBAL_STORE.is_demoted(skill_id=skill_id)


def get_skill_stats(skill_id: Optional[str] = None) -> Dict[str, Any]:
    """Get outcome stats for a specific skill or all skills."""
    return _GLOBAL_STORE.get_stats(skill_id=skill_id)


def promote_skill(skill_id: str) -> Dict[str, Any]:
    """Manually re-promote a demoted skill."""
    return _GLOBAL_STORE.promote_skill(skill_id=skill_id)
