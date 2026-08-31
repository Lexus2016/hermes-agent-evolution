#!/usr/bin/env python3
"""Staged Mem0 health probe — attribute a red memory stack to the failing stage.

The legacy probe in memory_system_health.sh ran one monolithic ``m.search()``
under ``timeout 60``: a timeout told us only "something hung", not whether the
bottleneck was the embedder (Ollama), the vector-store write (Qdrant), or the
read path. This probe runs each stage separately, times it, and emits one
diagnostic JSON line per stage, so a hung probe can be attributed to a
concrete component — and a persistently red probe escalates instead of
silently accumulating dumps (#167).

Stages (each independently timed and attributed):
  init   — ``Memory.from_config`` (client construction only)
  embed  — embedder call (Ollama nomic-embed-text by default)
  write  — ``m.add()`` (embed + vector-store upsert)
  read   — ``m.search()`` (vector query + optional rerank)

A stage line is printed when the stage STARTS and again when it finishes, so
even a hard ``timeout`` kill leaves the last ``started`` line as the
attribution: the stage that was in flight when the probe died.

Exit codes: 0 = all stages ok; 1 = one or more stages failed; 2 = config error.

Import-safe (mem0 imported lazily inside the CLI path only) so the pure
orchestration logic is unit-testable with a stub memory object.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

PROBE_QUERY = "health probe"
PROBE_USER_ID = "__health_probe__"
DEFAULT_CONFIG = "mem0.json"
DEFAULT_TIMEOUT = 60.0


@dataclass
class StageResult:
    """Outcome of one probe stage."""

    stage: str
    ok: bool
    seconds: float
    detail: str = ""


@dataclass
class ProbeOutcome:
    """All stage outcomes plus the overall verdict."""

    results: List[StageResult] = field(default_factory=list)
    ok: bool = True

    def failing_stages(self) -> List[str]:
        return [r.stage for r in self.results if not r.ok]


def _run_one(
    stage: str, fn: Callable[[], Any], emit: Callable[[dict], None]
) -> StageResult:
    """Time one stage, emitting a started line before and a finished line after.

    The started line is emitted FIRST so a hard kill of the whole probe leaves
    attribution: the last line on stdout names the stage that was in flight.
    """
    emit({"stage": stage, "status": "started"})
    start = time.monotonic()
    try:
        fn()
    except Exception as exc:  # noqa: BLE001 — any failure must be attributed
        elapsed = time.monotonic() - start
        res = StageResult(
            stage=stage,
            ok=False,
            seconds=elapsed,
            detail=f"{type(exc).__name__}: {exc}",
        )
        emit({
            "stage": stage,
            "status": "failed",
            "seconds": round(elapsed, 3),
            "detail": res.detail,
        })
        return res
    elapsed = time.monotonic() - start
    emit({"stage": stage, "status": "ok", "seconds": round(elapsed, 3)})
    return StageResult(stage=stage, ok=True, seconds=elapsed)


def run_probe(
    memory: Any,
    query: str = PROBE_QUERY,
    user_id: str = PROBE_USER_ID,
    emit: Callable[[dict], None] = lambda d: None,
) -> ProbeOutcome:
    """Run the staged probe against a memory-like object.

    ``memory`` may be any object exposing ``embedding_model.embed`` (optional —
    the embed stage is skipped when absent), ``add`` and ``search``. This keeps
    the orchestration testable without a live mem0 stack.
    """
    outcome = ProbeOutcome()

    def _stage(stage: str, fn: Callable[[], Any]) -> None:
        res = _run_one(stage, fn, emit)
        outcome.results.append(res)
        if not res.ok:
            outcome.ok = False

    _stage("init", lambda: getattr(memory, "_probe_initialized", lambda: None)())

    embedder = getattr(memory, "embedding_model", None)
    if embedder is not None and hasattr(embedder, "embed"):
        _stage("embed", lambda: embedder.embed(query))

    if hasattr(memory, "add"):
        _stage("write", lambda: memory.add(query, user_id=user_id))

    if hasattr(memory, "search"):
        _stage(
            "read",
            lambda: memory.search(query, limit=1, filters={"user_id": user_id}),
        )

    return outcome


def _load_oss_config(path: str) -> Dict[str, Any]:
    """Load the OSS block of a mem0.json config file."""
    import json as _json

    with open(path, encoding="utf-8") as fh:
        cfg = _json.load(fh)
    oss = cfg.get("oss") if isinstance(cfg, dict) else None
    if not isinstance(oss, dict):
        raise ValueError(
            f"config at {path} has no 'oss' dict (got {type(cfg).__name__})"
        )
    return oss


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description=(__doc__ or "Staged Mem0 health probe").splitlines()[0]
    )
    parser.add_argument(
        "--config",
        default=DEFAULT_CONFIG,
        help=f"path to mem0.json (default: {DEFAULT_CONFIG})",
    )
    parser.add_argument(
        "--user-id", default=PROBE_USER_ID, help="namespace for probe writes"
    )
    parser.add_argument("--query", default=PROBE_QUERY, help="probe query text")
    args = parser.parse_args(argv)

    def emit(line: dict) -> None:
        print(json.dumps(line, separators=(",", ":")), flush=True)

    try:
        oss = _load_oss_config(args.config)
    except Exception as exc:  # noqa: BLE001 — config errors exit 2
        emit({"stage": "config", "status": "failed", "detail": str(exc)})
        return 2

    try:
        from mem0 import Memory

        memory = Memory.from_config({
            "vector_store": oss["vector_store"],
            "llm": oss["llm"],
            "embedder": oss["embedder"],
            "reranker": oss.get("reranker"),
            "version": "v1.1",
        })
    except Exception as exc:  # noqa: BLE001 — init failure must be attributed
        emit({
            "stage": "init",
            "status": "failed",
            "detail": f"{type(exc).__name__}: {exc}",
        })
        return 1

    outcome = run_probe(memory, query=args.query, user_id=args.user_id, emit=emit)
    emit({"probe": "done", "ok": outcome.ok, "failed": outcome.failing_stages()})
    return 0 if outcome.ok else 1


if __name__ == "__main__":
    sys.exit(main())
