"""Pre-flight provider check + cached digest fallback for evolution cron jobs.

The evolution pipeline (introspection → analysis → implementation → research →
funnel → integration) runs as regular cron agent sessions. When the configured
provider is unreachable, those sessions burn retries/timeouts before producing
zero deliverables. This module provides a lightweight ping and a fallback to
the most recent on-disk digest so the pipeline can keep moving with stale but
useful input instead of failing silently.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any, Dict, Optional

from hermes_constants import get_hermes_home
from hermes_cli.config import load_config_readonly
from hermes_cli.timeouts import get_provider_request_timeout

logger = logging.getLogger(__name__)

# Stages in the evolution pipeline and the file extension each one writes.
_EVOLUTION_STAGES = {
    "introspection": ".json",
    "analysis": ".json",
    "implementation": ".md",
    "research": ".md",
    "funnel": ".md",
    "integration": ".md",
}


def evolution_job_stage(job: Dict[str, Any]) -> Optional[str]:
    """Return the evolution stage for a cron job, or None if it is not an
    evolution pipeline job.

    Matches job names like ``evolution-introspection`` or tags that include
    ``evolution`` plus a known stage name.
    """
    name = str(job.get("name") or job.get("id") or "").lower()
    tags = job.get("tags")
    tags_lower = {str(t).lower() for t in tags} if isinstance(tags, list) else set()

    if not name.startswith("evolution-") and not name.startswith("evolution") and "evolution" not in tags_lower:
        return None

    for stage in _EVOLUTION_STAGES:
        if stage in name:
            return stage

    for stage in _EVOLUTION_STAGES:
        if stage in tags_lower:
            return stage

    return None


def _evolution_dir(hermes_home: Optional[Path] = None) -> Path:
    home = (hermes_home or get_hermes_home()).resolve()
    return home / "evolution"


def _preflight_timeout_seconds(cfg: Optional[Any] = None) -> float:
    """Return the configured pre-flight timeout in seconds (default 30)."""
    if cfg is None:
        try:
            cfg = load_config_readonly() or {}
        except Exception:
            cfg = {}
    cron_cfg = cfg.get("cron", {}) if isinstance(cfg, dict) else {}
    if not isinstance(cron_cfg, dict):
        cron_cfg = {}
    raw = cron_cfg.get("preflight_timeout_seconds", 30.0)
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return 30.0
    if value <= 0:
        return 30.0
    return value


def _preflight_enabled(cfg: Optional[Any] = None) -> bool:
    """Return whether pre-flight checks are enabled (default True)."""
    if cfg is None:
        try:
            cfg = load_config_readonly() or {}
        except Exception:
            cfg = {}
    cron_cfg = cfg.get("cron", {}) if isinstance(cfg, dict) else {}
    if not isinstance(cron_cfg, dict):
        cron_cfg = {}
    return str(cron_cfg.get("preflight_enabled", "true")).lower() not in {
        "false",
        "0",
        "no",
        "off",
        "disabled",
    }


def find_latest_digest(
    stage: str, hermes_home: Optional[Path] = None
) -> Optional[Path]:
    """Return the most recent digest file for an evolution stage, or None."""
    if stage not in _EVOLUTION_STAGES:
        return None
    ext = _EVOLUTION_STAGES[stage]
    stage_dir = _evolution_dir(hermes_home) / stage
    if not stage_dir.is_dir():
        return None
    candidates = sorted(
        (p for p in stage_dir.iterdir() if p.is_file() and p.suffix == ext),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return candidates[0] if candidates else None


def load_digest_as_fallback(
    stage: str,
    hermes_home: Optional[Path] = None,
    *,
    max_chars: int = 200_000,
) -> Optional[str]:
    """Load the most recent on-disk digest for a stage, bounded in size."""
    path = find_latest_digest(stage, hermes_home)
    if path is None:
        return None
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception as exc:
        logger.warning("Could not read cached digest %s: %s", path, exc)
        return None
    if len(text) > max_chars:
        text = text[:max_chars] + "\n\n[truncated: stale digest exceeded size limit]"
    header = (
        f"⚠️ Provider unreachable for '{stage}' cron job. "
        f"Using cached digest from {path.name} instead.\n\n"
    )
    return header + text


def _provider_specific_timeout(runtime: Dict[str, Any], cfg: Optional[Any]) -> float:
    """Pick the tightest sensible timeout for the provider ping."""
    provider = runtime.get("provider") or ""
    model = runtime.get("model") or ""
    configured = get_provider_request_timeout(provider, model)
    if configured is not None and configured > 0:
        return configured
    return _preflight_timeout_seconds(cfg)


def preflight_provider(
    runtime: Dict[str, Any], *, cfg: Optional[Any] = None
) -> Optional[str]:
    """Run a minimal, non-streaming provider ping.

    Returns None on success, or a short human-readable error string on failure.
    This is intentionally lightweight: a single-turn request with max_tokens=1.
    """
    api_key = runtime.get("api_key") or ""
    base_url = runtime.get("base_url") or ""
    provider = runtime.get("provider") or ""
    api_mode = runtime.get("api_mode") or "chat_completions"
    model = runtime.get("model") or ""
    command = runtime.get("command")

    if not api_key and not command:
        return "no API key or ACP command available for pre-flight ping"

    if not model and not command:
        return "no model configured for pre-flight ping"

    timeout = _provider_specific_timeout(runtime, cfg)

    try:
        if command or api_mode == "copilot-acp":
            # ACP providers are subprocess-based; a real ping would require
            # spawning the ACP helper. For now treat them as reachable if the
            # runtime resolved (auth setup succeeded). A dedicated ACP ping can
            # be added later without changing the scheduler contract.
            return None

        if api_mode == "anthropic_messages":
            return _preflight_anthropic(api_key, base_url, model, timeout)
        if api_mode == "bedrock_converse":
            return _preflight_bedrock(runtime, timeout)
        return _preflight_openai_compatible(api_key, base_url, model, timeout, provider)
    except Exception as exc:
        logger.debug("Pre-flight ping raised %s: %s", type(exc).__name__, exc)
        return f"pre-flight ping failed: {type(exc).__name__}: {exc}"


def _preflight_openai_compatible(
    api_key: str,
    base_url: str,
    model: str,
    timeout: float,
    provider: str,
) -> Optional[str]:
    from openai import OpenAI

    client_kwargs: Dict[str, Any] = {"api_key": api_key, "timeout": timeout}
    if base_url:
        client_kwargs["base_url"] = base_url
    client = OpenAI(**client_kwargs)
    start = time.time()
    try:
        client.chat.completions.create(
            model=model or "default",
            messages=[{"role": "user", "content": "ping"}],
            max_tokens=1,
            stream=False,
        )
        elapsed = time.time() - start
        logger.debug("Pre-flight ping to %s succeeded in %.2fs", provider, elapsed)
        return None
    finally:
        try:
            client.close()
        except Exception:
            pass


def _preflight_anthropic(
    api_key: str, base_url: str, model: str, timeout: float
) -> Optional[str]:
    from anthropic import Anthropic

    client_kwargs: Dict[str, Any] = {"api_key": api_key, "timeout": timeout}
    if base_url:
        client_kwargs["base_url"] = base_url
    client = Anthropic(**client_kwargs)
    start = time.time()
    try:
        client.messages.create(
            model=model or "claude-3-5-haiku-latest",
            max_tokens=1,
            messages=[{"role": "user", "content": "ping"}],
        )
        elapsed = time.time() - start
        logger.debug("Pre-flight ping to anthropic succeeded in %.2fs", elapsed)
        return None
    finally:
        try:
            client.close()
        except Exception:
            pass


def _preflight_bedrock(runtime: Dict[str, Any], timeout: float) -> Optional[str]:
    # Bedrock uses boto3; resolving the runtime already validates credentials.
    # A full converse ping would require a model id and may incur token cost,
    # so we treat the resolved runtime as reachable. This preserves the fallback
    # contract while avoiding unexpected Bedrock charges.
    _ = timeout
    _ = runtime
    return None
