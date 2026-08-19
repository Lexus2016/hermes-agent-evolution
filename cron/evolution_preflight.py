"""Pre-flight provider check + cached digest fallback for evolution cron jobs.

The evolution pipeline (introspection → analysis → implementation → research →
funnel → integration) runs as regular cron agent sessions. When the configured
provider is unreachable, those sessions burn retries/timeouts before producing
zero deliverables. This module provides a lightweight ping and a fallback to
the most recent on-disk digest so the pipeline can keep moving with stale but
useful input instead of failing silently.
"""

from __future__ import annotations

import json
import logging
import os
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

# Stages that spawn an expensive LLM agent and must respect the pipeline
# halt-state file written by scripts/evolution_funnel.py (merged=0 for 5+
# cycles AND selected=0 for 3+ cycles). `funnel` is a deterministic no_agent
# job that MUST keep running every cycle regardless of halt state — it is
# what measures recovery and clears halt-state.txt once metrics improve.
# `integration` is intentionally left ungated for now (#913).
_HALT_GATED_STAGES = frozenset({
    "introspection",
    "analysis",
    "implementation",
    "research",
})


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


def _halt_state_active(hermes_home: Optional[Path] = None) -> bool:
    """Return whether the evolution pipeline halt-state file is present.

    ``scripts/evolution_funnel.py::is_evolution_halted()`` (the writer) and
    ``scripts/evolution_hydra_gate.py::_check_halt()`` resolve their
    directory as ``EVOLUTION_PROFILE_DIR`` if set, else ``~/.hermes/evolution``
    — independently of ``HERMES_HOME``, since those run as standalone script
    copies under ``HERMES_HOME/scripts`` and cannot rely on importing this
    package. Mirror that resolution EXACTLY (not "check both"): if
    ``EVOLUTION_PROFILE_DIR`` is set, it is the writer's one and only
    location, so it is the one and only location checked here too — falling
    through to :func:`_evolution_dir` in that case would risk a false
    "halted" from an unrelated stale ``halt-state.txt`` left over in a
    different ``HERMES_HOME`` tree. Only when ``EVOLUTION_PROFILE_DIR`` is
    unset do we fall back to :func:`_evolution_dir` — the same
    ``HERMES_HOME``-based resolution the scheduler already uses for
    ``load_digest_as_fallback``/``find_latest_digest``, and which matches
    the writer's own default in that case.

    Fail-safe: ANY error while resolving a path or checking existence is
    treated as NOT halted — a broken halt check must never wrongly skip a
    job (#913).
    """
    try:
        profile_dir = os.environ.get("EVOLUTION_PROFILE_DIR", "").strip()
        if profile_dir:
            halt_dir = Path(profile_dir).expanduser().resolve()
        else:
            halt_dir = _evolution_dir(hermes_home)
        return (halt_dir / "halt-state.txt").exists()
    except OSError:
        return False
    except Exception as exc:  # pragma: no cover - defense in depth
        logger.debug("Halt-state check failed, treating as not halted: %s", exc)
        return False


def should_skip_for_halt(
    stage: Optional[str], hermes_home: Optional[Path] = None
) -> bool:
    """Return True if a cron job for ``stage`` should be skipped because the
    evolution pipeline is structurally halted.

    Only the expensive LLM-agent stages in :data:`_HALT_GATED_STAGES`
    (introspection, analysis, implementation, research) are gated — this
    extends the halt-state gate that already covers the Hydra orchestrator
    (``evolution_hydra_gate.py``) to the individual stage crons that spawn
    their own agents directly, so a structurally-halted pipeline stops
    burning tokens on every stage, not only Hydra (#913). ``funnel`` and
    ``integration`` are never skipped here.
    """
    if stage not in _HALT_GATED_STAGES:
        return False
    return _halt_state_active(hermes_home)


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
    """Return the most recent digest file for an evolution stage, or None.

    Digest filenames follow a sortable date-encoded convention
    (``YYYY-MM-DD.json`` / ``.md``, optionally with ``-pass<N>`` / ``-tick<N>``
    suffixes).  Sorting by filename instead of ``st_mtime`` avoids flaky
    results when files are touched or copied after creation (#1767).
    """
    if stage not in _EVOLUTION_STAGES:
        return None
    ext = _EVOLUTION_STAGES[stage]
    stage_dir = _evolution_dir(hermes_home) / stage
    if not stage_dir.is_dir():
        return None
    candidates = sorted(
        (p for p in stage_dir.iterdir() if p.is_file() and p.suffix == ext),
        key=lambda p: p.name,
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


# ── Provider-balance preflight (HTTP 402) ────────────────────────────────
# HTTP 402 Insufficient Balance is NON-retryable billing exhaustion: every
# agent wake immediately fails, burning scheduler cycles and spamming
# errors.log (08-19 incident: 50 cron failures across 6 jobs, 6.5h pipeline
# outage). The scheduler's preflight chain must detect an exhausted account
# and suppress wake attempts instead of launching agents that will 402.
#
# Balance state is cached on disk with a TTL so the check is cheap (one
# max_tokens=1 ping per TTL window, not per wake) and survives restarts.
# A `BALANCE_LOW` marker in jobs.json is published by the scheduler when
# the preflight returns the reason below (see scheduler._preflight_job_config).

#: Distinct marker embedded in the preflight error so the scheduler can
#: classify HTTP 402 (billing exhaustion) apart from other ping failures.
BALANCE_LOW_ERROR_MARKER = "HTTP 402 Insufficient Balance"

#: Default TTL for the on-disk balance-low marker (seconds).
_BALANCE_LOW_TTL_SECONDS = 15 * 60  # 15 min


def _balance_cache_path(hermes_home: Optional[Path] = None) -> Path:
    return _evolution_dir(hermes_home) / "balance-low.json"


def _balance_state(hermes_home: Optional[Path] = None) -> Dict[str, Any]:
    """Read the balance-verdict marker file; never raises."""
    try:
        path = _balance_cache_path(hermes_home)
        if path.is_file():
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
    except Exception as exc:
        logger.debug("Could not read balance cache: %s", exc)
    return {}


def _balance_verdict(
    provider: str, *, hermes_home: Optional[Path] = None, now: Optional[float] = None
) -> Optional[str]:
    """Return the FRESH cached verdict for ``provider``: ``"ok"`` or
    ``"low"``, or None when no fresh verdict exists (cache miss / stale).

    Fail-safe: any read/parse error or missing marker means no verdict —
    a broken cache must never suppress jobs (#2872, same spirit as #913).
    """
    if not provider:
        return None
    marker = _balance_state(hermes_home).get(provider)
    if not isinstance(marker, dict):
        return None
    try:
        expires_at = float(marker.get("expires_at") or 0)
    except (TypeError, ValueError):
        return None
    now = time.time() if now is None else now
    if now >= expires_at:
        return None
    return marker.get("status") if marker.get("status") in ("ok", "low") else None


def provider_balance_low(
    provider: str, *, hermes_home: Optional[Path] = None, now: Optional[float] = None
) -> bool:
    """Return whether a FRESH balance-low verdict exists for ``provider``."""
    return _balance_verdict(provider, hermes_home=hermes_home, now=now) == "low"


def _set_provider_balance_verdict(
    provider: str,
    status: str,
    *,
    hermes_home: Optional[Path] = None,
    ttl_seconds: float = _BALANCE_LOW_TTL_SECONDS,
) -> None:
    """Persist a balance verdict for ``provider`` with a TTL."""
    if not provider or status not in ("ok", "low"):
        return
    try:
        state = _balance_state(hermes_home)
        state[provider] = {"status": status, "expires_at": time.time() + ttl_seconds}
        path = _balance_cache_path(hermes_home)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state, indent=2), encoding="utf-8")
    except Exception as exc:
        logger.debug("Could not persist balance verdict: %s", exc)


def mark_provider_balance_low(
    provider: str,
    *,
    hermes_home: Optional[Path] = None,
    ttl_seconds: float = _BALANCE_LOW_TTL_SECONDS,
) -> None:
    """Persist a balance-low verdict for ``provider`` with a TTL."""
    _set_provider_balance_verdict(provider, "low", hermes_home=hermes_home, ttl_seconds=ttl_seconds)


def mark_provider_balance_ok(
    provider: str,
    *,
    hermes_home: Optional[Path] = None,
    ttl_seconds: float = _BALANCE_LOW_TTL_SECONDS,
) -> None:
    """Persist a healthy-balance verdict so healthy accounts are NOT pinged
    on every wake — one ping per provider per TTL window across all jobs."""
    _set_provider_balance_verdict(provider, "ok", hermes_home=hermes_home, ttl_seconds=ttl_seconds)


def clear_provider_balance_low(
    provider: str, *, hermes_home: Optional[Path] = None
) -> None:
    """Clear the balance verdict for ``provider`` (forces a re-check)."""
    if not provider:
        return
    try:
        state = _balance_state(hermes_home)
        if provider in state:
            del state[provider]
            path = _balance_cache_path(hermes_home)
            path.write_text(json.dumps(state, indent=2), encoding="utf-8")
    except Exception as exc:
        logger.debug("Could not clear balance verdict: %s", exc)


def preflight_provider_balance(
    runtime: Dict[str, Any], *, cfg: Optional[Any] = None, hermes_home: Optional[Path] = None
) -> Optional[str]:
    """Provider-balance preflight: None when the account has balance (or the
    check cannot run), or a BALANCE_LOW reason when the account is exhausted.

    Cache-first: a fresh ``ok`` verdict short-circuits with NO ping (healthy
    accounts are not pinged on every wake — at most one ping per provider per
    TTL window); a fresh ``low`` verdict returns the reason WITHOUT a ping —
    repeated wake attempts are suppressed until the TTL expires (the 08-19
    incident shape). Otherwise the standard max_tokens=1 ping runs; an HTTP
    402 response marks balance low and returns the reason; success marks ok.
    Any other ping failure (network, timeout, 401) is NOT treated as balance
    exhaustion — it falls through to the caller's existing handling
    (fail-open) and forces a re-check on the next wake.
    """
    provider = runtime.get("provider") or ""
    verdict = _balance_verdict(provider, hermes_home=hermes_home)
    if verdict == "low":
        return (
            f"{BALANCE_LOW_ERROR_MARKER} — provider '{provider}' account "
            "balance is exhausted; waiting for balance (recheck on TTL expiry)"
        )
    if verdict == "ok":
        return None

    err = preflight_provider(runtime, cfg=cfg)
    if err is None:
        mark_provider_balance_ok(provider, hermes_home=hermes_home)
        return None
    if BALANCE_LOW_ERROR_MARKER in err:
        mark_provider_balance_low(provider, hermes_home=hermes_home)
        return (
            f"{BALANCE_LOW_ERROR_MARKER} — provider '{provider}' account "
            "balance is exhausted; waiting for balance (recheck on TTL expiry)"
        )
    return None


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
        try:
            client.chat.completions.create(
                model=model or "default",
                messages=[{"role": "user", "content": "ping"}],
                max_tokens=1,
                stream=False,
            )
        except Exception as exc:
            # Classify HTTP 402 Insufficient Balance (billing exhaustion)
            # distinctly — it is non-retryable and means every wake will
            # fail, so the scheduler must suppress jobs rather than treat
            # it as a transient ping failure (#2872).
            if _is_http_status(exc, 402):
                return f"{BALANCE_LOW_ERROR_MARKER}: {exc}"
            raise
        elapsed = time.time() - start
        logger.debug("Pre-flight ping to %s succeeded in %.2fs", provider, elapsed)
        return None
    finally:
        try:
            client.close()
        except Exception:
            pass


def _is_http_status(exc: Exception, status: int) -> bool:
    """Return whether ``exc`` carries an HTTP status code (402 etc.).

    Handles OpenAI/Anthropic SDK ``APIStatusError`` (``status_code``), the
    underlying ``httpx`` errors (``response.status_code``), and the generic
    ``RuntimeError: HTTP 402`` shape the scheduler logs when a provider
    returns an error mid-run.
    """
    for attr in ("status_code", "code"):
        value = getattr(exc, attr, None)
        if value is not None:
            try:
                if int(value) == status:
                    return True
            except (TypeError, ValueError):
                pass
    response = getattr(exc, "response", None)
    if response is not None:
        value = getattr(response, "status_code", None)
        if value is not None:
            try:
                if int(value) == status:
                    return True
            except (TypeError, ValueError):
                pass
    return f"HTTP {status}" in str(exc)


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
