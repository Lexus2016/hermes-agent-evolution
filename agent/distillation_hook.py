"""Contrastive trajectory distillation hook (issue #2235, Slice B).

Extracts a **model-agnostic invariant** from a contrasting pair of
trajectories that tackle the same problem but diverge — typically one
succeeds and the other fails. The invariant is what the successful path did
that the failed path did not (or a violation the failed path committed),
stated in task terms rather than model terms, so it generalizes across
model families.

This is a **standalone module** — it reads structured trajectory data
(ShareGPT-style, as written by ``agent/trajectory.py::save_trajectory``),
uses the model-identity metadata introduced by Slice A (#2234,
``agent/tqmemory_model_filter.py`` / ``adversarial_verification.detect_model_family``)
to tag invariants with their source families, and writes distilled invariants
to a JSONL store. No changes to the existing memory or trajectory pipeline.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

logger = logging.getLogger(__name__)

# Failure-vs-success discrimination: tokens that mark a step as a corrective
# / verifying action (present in good trajectories) vs. a stall (retry spam /
# repeated identical failures) that plagues bad ones.
_CORRECTIVE_RE = re.compile(
    r"\b(check|verify|confirm|test|assert|validate|reproduc|double-check"
    r"|trace|debug|inspect|review)\b",
    re.IGNORECASE,
)
_STALL_RE = re.compile(
    r"\b(retry|again|re-?try|timeout|error|failed|still|same)\b",
    re.IGNORECASE,
)


@dataclass
class DistilledInvariant:
    """A model-agnostic reasoning invariant distilled from a contrasting pair.

    Attributes:
        text: the canonical, natural-language invariant (model-agnostic).
        dimension: coarse task dimension (see model_routing_table.TASK_DIMENSIONS).
        approach: the retained approach (which trajectory it came from).
        source_models: model ids of the two contrasted trajectories.
        source_families: distinct families among source_models (#2234 metadata).
        confidence: 0..1 heuristic confidence in the invariant.
        timestamp: ISO-8601 write time.
    """

    text: str
    dimension: str
    approach: str
    source_models: List[str]
    source_families: List[str]
    confidence: float
    timestamp: str

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "DistilledInvariant":
        return cls(
            text=str(d.get("text", "")),
            dimension=str(d.get("dimension", "general")),
            approach=str(d.get("approach", "")),
            source_models=list(d.get("source_models", [])),
            source_families=list(d.get("source_families", [])),
            confidence=float(d.get("confidence", 0.0)),
            timestamp=str(d.get("timestamp", "")),
        )


def _dimension_of(task: Dict[str, Any]) -> str:
    """Classify a task into a coarse dimension (mirrors routing table)."""
    for mod in ("tools.model_routing_table", "model_routing_table"):
        try:
            from importlib import import_module

            return import_module(mod).classify_task(task)
        except Exception:
            continue
    return "general"


def _extract_steps(trajectory: Iterable[Dict[str, Any]]) -> List[Dict[str, str]]:
    """Flatten a ShareGPT trajectory into role+content steps."""
    steps: List[Dict[str, str]] = []
    for msg in trajectory or []:
        if not isinstance(msg, dict):
            continue
        role = str(msg.get("role", "")).lower()
        content = msg.get("content") or msg.get("text") or ""
        if not isinstance(content, str):
            content = json.dumps(content) if content is not None else ""
        if not content.strip():
            continue
        steps.append({"role": role, "content": content.strip()})
    return steps


def _corrective_score(steps: List[Dict[str, str]]) -> float:
    """Ratio of corrective/verifying assistant steps to the total — a
    model-agnostic proxy for how grounded the approach is."""
    total = 0
    corrective = 0
    for st in steps:
        if st["role"] != "assistant":
            continue
        total += 1
        if _CORRECTIVE_RE.search(st["content"]):
            corrective += 1
    return (corrective / total) if total else 0.0


def _stall_score(steps: List[Dict[str, str]]) -> float:
    """Ratio of stall-heavy assistant steps (retry/timeout/error vocabulary)
    to total — high in failing trajectories that loop on the same failure."""
    total = 0
    stalls = 0
    for st in steps:
        if st["role"] != "assistant":
            continue
        total += 1
        if _STALL_RE.search(st["content"]):
            stalls += 1
    return (stalls / total) if total else 0.0


def _groundedness(steps: List[Dict[str, str]]) -> float:
    """Combined grounding signature: corrective minus stall, in [-1, 1]."""
    return _corrective_score(steps) - _stall_score(steps)


def _pick_approach(a: List[Dict[str, str]], b: List[Dict[str, str]]) -> str:
    """Return the label of the more grounded trajectory ('a' or 'b')."""
    return "a" if _groundedness(a) >= _groundedness(b) else "b"


def _distill_text(
    task: Dict[str, Any],
    kept: List[Dict[str, str]],
    dropped: List[Dict[str, str]],
    dimension: str,
) -> str:
    """Compose a canonical, model-agnostic invariant string.

    The invariant states the retained grounding behavior and, when a stall
    signal is present, the violation the dropped trajectory committed.
    """
    corrective = sum(
        1
        for s in kept
        if s["role"] == "assistant" and _CORRECTIVE_RE.search(s["content"])
    )
    violations = sorted({
        m.lower()
        for s in dropped
        if s["role"] == "assistant"
        for m in _STALL_RE.findall(s["content"])
    })
    task_type = str(task.get("type") or dimension)
    parts = [
        f"[{dimension}] For task type '{task_type}', verify before finalizing "
        f"({corrective} corrective step(s) observed in the retained approach).",
    ]
    if violations:
        parts.append(
            "Avoid: "
            + ", ".join(sorted(set(violations))[:4])
            + " (observed in the dropped approach)."
        )
    return " ".join(parts)


def contrast_trajectories(
    task: Dict[str, Any],
    trajectory_a: Iterable[Dict[str, Any]],
    model_a: str,
    trajectory_b: Iterable[Dict[str, Any]],
    model_b: str,
    *,
    _now: Optional[str] = None,
) -> DistilledInvariant:
    """Distill a model-agnostic invariant from two contrasting trajectories.

    ``trajectory_a``/``trajectory_b`` are ShareGPT-style lists (role/content),
    optionally tagged with ``model_identity`` metadata per #2234. The two are
    contrasted by their grounding signature (corrective-vs-stall); the more
    grounded approach becomes the retained invariant and its contrasting
    violations are recorded as what to avoid.

    Returns a non-empty ``DistilledInvariant`` (never raises).
    """
    a_steps = _extract_steps(trajectory_a)
    b_steps = _extract_steps(trajectory_b)
    dimension = _dimension_of(task)
    approach = _pick_approach(a_steps, b_steps)
    kept, dropped = (a_steps, b_steps) if approach == "a" else (b_steps, a_steps)

    text = (
        _distill_text(task, kept, dropped, dimension)
        if kept
        else (f"[{dimension}] No assistant steps to contrast.")
    )

    families: List[str] = []
    for model in (model_a, model_b):
        try:
            from agent.adversarial_verification import detect_model_family  # type: ignore

            fam = detect_model_family(model)
        except Exception:
            fam = "unknown"
        if fam and fam != "unknown" and fam not in families:
            families.append(fam)

    # Confidence reflects the STRENGTH OF THE CONTRAST: how much more grounded
    # the retained approach is than the dropped one. A large gap (a sharply
    # divergent pair where the good approach is clearly more grounded) yields
    # high confidence. Bounded to [0.1, 0.99]; never raises.
    gap = _groundedness(kept) - _groundedness(dropped)
    confidence = 0.5 + 0.5 * gap
    confidence = max(0.1, min(0.99, round(confidence, 3)))

    return DistilledInvariant(
        text=text,
        dimension=dimension,
        approach=approach,
        source_models=[model_a, model_b],
        source_families=families,
        confidence=confidence,
        timestamp=_now or datetime.now(timezone.utc).isoformat(),
    )


def _store_path(store_dir: Optional[Path]) -> Path:
    if store_dir is not None:
        return store_dir / "distilled-invariants.jsonl"
    env = os.environ.get("EVOLUTION_PROFILE_DIR", "")
    base = Path(env) if env else Path.home() / ".hermes" / "evolution"
    return base / "distilled-invariants.jsonl"


def store_invariant(
    invariant: DistilledInvariant, store_dir: Optional[Path] = None
) -> bool:
    """Append one distilled invariant as a JSON line. Idempotent per text:
    replaces an existing line with the same text so re-distillation doesn't
    duplicate. Returns True on success, False on any I/O error."""
    path = _store_path(store_dir)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        lines = []
        if path.exists():
            for ln in path.read_text(encoding="utf-8").splitlines():
                ln = ln.strip()
                if not ln:
                    continue
                try:
                    if json.loads(ln).get("text") == invariant.text:
                        continue  # dedupe
                except (ValueError, TypeError):
                    pass
                lines.append(ln)
        lines.append(json.dumps(invariant.to_dict(), sort_keys=True))
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return True
    except OSError as exc:
        logger.warning("store_invariant failed: %s", exc)
        return False


def load_invariants(store_dir: Optional[Path] = None) -> List[DistilledInvariant]:
    """Read all distilled invariants oldest-first, skipping malformed lines."""
    path = _store_path(store_dir)
    out: List[DistilledInvariant] = []
    if not path.exists():
        return out
    for ln in path.read_text(encoding="utf-8").splitlines():
        ln = ln.strip()
        if not ln:
            continue
        try:
            out.append(DistilledInvariant.from_dict(json.loads(ln)))
        except (ValueError, TypeError, KeyError):
            continue
    return out


def retrieve(
    task: Dict[str, Any], store_dir: Optional[Path] = None, limit: int = 3
) -> List[DistilledInvariant]:
    """Return the most-confident invariants relevant to ``task``'s dimension."""
    dim = _dimension_of(task)
    cands = [i for i in load_invariants(store_dir) if i.dimension == dim]
    cands.sort(key=lambda i: i.confidence, reverse=True)
    return cands[:limit]
