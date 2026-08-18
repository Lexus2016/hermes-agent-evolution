# -*- coding: utf-8 -*-
"""AgentOpt Slice 1 — opt-in LLM-call telemetry (#2741).

Child of #2695. Opt-in via ``config.yaml -> agentopt.telemetry.enabled`` ONLY
(no env vars), disabled by default. When on, each auxiliary LLM call appends
one content-free JSONL record (model, tool, step, cost, latency_ms, outcome);
off = byte-identical. Fail-closed, content-free (payloads never captured).
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Dict, Optional

_FNAME = "agentopt-llm-calls.jsonl"


def _store_path() -> Path:
    override = os.environ.get("AGENTOPT_TELEMETRY_STORE", "").strip()
    if override:
        return Path(override)
    env = os.environ.get("EVOLUTION_PROFILE_DIR", "").strip()
    if env:
        return Path(env) / _FNAME
    hh = os.environ.get("HERMES_HOME", "").strip()
    return (
        Path(hh) / "evolution" / _FNAME
        if hh
        else Path.home() / ".hermes" / "evolution" / _FNAME
    )


def agentopt_enabled(config: Optional[Dict[str, Any]] = None) -> bool:
    """Opt-in flag from config.yaml; default False; fail-closed. Injectable."""
    if config is not None:
        section = config.get("agentopt")
        tel = section.get("telemetry") if isinstance(section, dict) else None
        return bool(isinstance(tel, dict) and tel.get("enabled", False))
    try:
        from hermes_cli.config import load_config_readonly

        return agentopt_enabled(load_config_readonly())
    except Exception:
        return False


def estimate_cost(usage: Any) -> Optional[float]:
    """Token-based USD estimate; None when usage absent (no fabricated cost)."""
    if hasattr(usage, "prompt_tokens"):
        prompt = int(getattr(usage, "prompt_tokens", 0) or 0)
        completion = int(getattr(usage, "completion_tokens", 0) or 0)
    elif isinstance(usage, dict):
        prompt = int(usage.get("prompt_tokens") or 0)
        completion = int(usage.get("completion_tokens") or 0)
    else:
        return None
    return round((prompt + completion) * 0.002 / 1000.0, 6)


def append_record(record: Dict[str, Any], store: Optional[Path] = None) -> bool:
    """Append one JSONL line. Best-effort; never raises."""
    if not isinstance(record.get("model"), str) or not record.get("model"):
        return False
    record.setdefault("ts", time.time())
    record.setdefault("latency_ms", 0.0)
    record.setdefault("outcome", "unknown")
    path = store or _store_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, sort_keys=True) + "\n")
        return True
    except OSError:
        return False


def record_llm_call(
    *,
    model: str,
    tool: str,
    step: str,
    latency_ms: float,
    outcome: str,
    usage: Any = None,
    store: Optional[Path] = None,
    config: Optional[Dict[str, Any]] = None,
) -> bool:
    """Record one LLM call when telemetry is enabled; no-op otherwise."""
    if not agentopt_enabled(config):
        return False
    rec: Dict[str, Any] = {
        "model": model,
        "tool": tool,
        "step": step,
        "latency_ms": round(float(latency_ms), 3),
        "outcome": outcome,
    }
    if usage is not None:
        est = estimate_cost(usage)
        if est is not None:
            rec["cost"] = est
    return append_record(rec, store=store)
