"""Credential masking for terminal / tool execution output (#2435).

Wires the existing ``agent.redact`` machinery to the tool-result choke point
so that *every* string a tool returns (terminal output, file reads, search
results, error messages) is redacted before it enters the model's context.

The heavy lifting lives in ``agent.redact`` — this module is a thin,
config-gated adapter that keeps the gateway/CLI surfaces consistent:

- ``masking_enabled()`` reads ``security.credential_masking`` from config.yaml
  (NOT a new ``HERMES_*`` env var — AGENTS.md: ``.env`` is for secrets only).
- ``mask_credentials(text)`` applies ``agent.redact.redact_sensitive_text``.
- ``mask_tool_output(value)`` is the single entry point the registry choke
  point calls for string results; it is a total function (never raises) and,
  when masking is disabled, a byte-identical passthrough.

Default is **off** so every existing test fixture remains byte-identical.
Masking must never break a tool result: any failure degrades to the unmasked
string rather than surfacing an error into the model context.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

# Cache the config gate so we don't re-read + re-parse config on every tool
# result (a hot path: one call per tool dispatch). Reset via reset_cache().
_config_cache: dict = {"enabled": None}


def _read_masking_enabled() -> bool:
    """Read ``security.credential_masking`` from config.yaml.

    Returns False on any failure (unreadable config, missing key, wrong type):
    masking is an opt-in safety net, so fail-open rather than fail-closed on
    config errors — but *never* fail into masking-off silently at runtime;
    a mismatch is logged once.
    """
    try:
        from hermes_cli.config import load_config

        cfg = load_config() or {}
        security = cfg.get("security", {}) or {}
        value = security.get("credential_masking", False)
        return bool(value)
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("credential masking config read failed: %s", exc)
        return False


def masking_enabled() -> bool:
    """Return whether credential masking is currently enabled."""
    if _config_cache["enabled"] is None:
        _config_cache["enabled"] = _read_masking_enabled()
    return _config_cache["enabled"]


def reset_cache() -> None:
    """Force the config gate to be re-read on the next ``masking_enabled()``.

    Intended for tests and for config-reload boundaries.
    """
    _config_cache["enabled"] = None


def mask_credentials(text: str) -> str:
    """Redact credentials from ``text`` using the core redaction engines.

    A thin wrapper over ``agent.redact.redact_sensitive_text``. Returns the
    input unchanged (never raises) when redaction is unavailable or fails.
    """
    if not isinstance(text, str) or not text:
        return text
    try:
        from agent.redact import redact_sensitive_text

        return redact_sensitive_text(text)
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("credential masking redaction failed: %s", exc)
        return text


def mask_tool_output(value: Any) -> Any:
    """Mask credentials in a tool result value.

    - When masking is disabled → byte-identical passthrough.
    - Strings → ``mask_credentials``.
    - Dicts / lists → ``mask_credentials`` applied to every ``str`` leaf
      (preserves structure; only text can carry a token-shaped secret).
    - Anything else → passthrough.

    Never raises: masking must not break a tool result.
    """
    if not masking_enabled():
        return value
    try:
        if isinstance(value, str):
            return mask_credentials(value)
        if isinstance(value, dict):
            return {k: mask_tool_output(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [mask_tool_output(v) for v in value]
        return value
    except Exception:  # pragma: no cover - defensive
        return value
