"""Serializable credential-pool row and its provider-specific runtime projection."""
from __future__ import annotations
import uuid
from dataclasses import dataclass, fields
from typing import Any, Optional
import hermes_cli.auth as auth_mod
from agent.credential_pool_timestamps import _parse_absolute_timestamp
from agent.credential_persistence import sanitize_borrowed_credential_payload
from agent.secret_scope import get_secret_str

AUTH_TYPE_OAUTH = "oauth"

AUTH_TYPE_API_KEY = "api_key"

SOURCE_MANUAL = "manual"

_EXTRA_KEYS = frozenset({
    "token_type", "scope", "client_id", "portal_base_url", "obtained_at",
    "expires_in", "agent_key_id", "agent_key_expires_in", "agent_key_reused",
    "agent_key_obtained_at", "tls", "secret_source", "secret_fingerprint",
    # Nous guest identity (``auth_method: anonymous``): the anon_ credential is the refresh material.
    "auth_method", "account_tier", "anon_token", "user_id", "org_id",
    # Classified failure semantics for the last exhaustion (agent/error_classifier.py).
    # Providers return 403 for both an edge throttle and a spending limit, so the
    # raw status cannot size a cooldown; persisted so a restart doesn't downgrade
    # a billing bench to a 60s transient cooldown.
    "failure_reason",
    "consecutive_failures",
})

_CLEAR_STATUS: dict[str, Any] = {
    "last_status": None,
    "last_status_at": None,
    "last_error_code": None,
    "last_error_reason": None,
    "last_error_message": None,
    "last_error_reset_at": None,
}

def _normalize_pool_auth_type(provider: str, token: Any, auth_type: Any) -> str:
    """Infer pool auth metadata for token formats with one unambiguous meaning."""
    if provider == "anthropic" and isinstance(token, str) and token.startswith("sk-ant-oat"):
        return AUTH_TYPE_OAUTH
    return str(auth_type or AUTH_TYPE_API_KEY)

@dataclass
class PooledCredential:
    provider: str
    id: str
    label: str
    auth_type: str
    priority: int
    source: str
    access_token: str
    refresh_token: Optional[str] = None
    last_status: Optional[str] = None
    last_status_at: Optional[float] = None
    last_error_code: Optional[int] = None
    last_error_reason: Optional[str] = None
    last_error_message: Optional[str] = None
    last_error_reset_at: Optional[float] = None
    # Epoch of the last deliberate ``hermes auth reset`` of this entry. Sticky: a later exhaustion
    # stamps a newer ``last_status_at``, so "reset postdates status" stays decidable across processes.
    status_cleared_at: Optional[float] = None
    base_url: Optional[str] = None
    expires_at: Optional[str] = None
    expires_at_ms: Optional[int] = None
    last_refresh: Optional[str] = None
    inference_base_url: Optional[str] = None
    agent_key: Optional[str] = None
    agent_key_expires_at: Optional[str] = None
    request_count: int = 0
    # A provider may rate-limit one model while the same credential remains
    # usable for its sibling models.  Keep that observation separate from the
    # credential-wide status used for auth and billing failures.
    model_cooldowns: Optional[dict[str, float]] = None
    extra: dict[str, Any] = None  # type: ignore[assignment]

    def __post_init__(self):
        if self.extra is None:
            self.extra = {}
        self.auth_type = _normalize_pool_auth_type(self.provider, self.access_token, self.auth_type)

    def __getattr__(self, name: str):
        if name in _EXTRA_KEYS:
            return self.extra.get(name)
        raise AttributeError(f"'{type(self).__name__}' object has no attribute {name!r}")

    @classmethod
    def from_dict(cls, provider: str, payload: dict[str, Any]) -> PooledCredential:
        field_names = {f.name for f in fields(cls) if f.name != "provider"}
        data = {k: payload.get(k) for k in field_names if k in payload}
        # Rehydrated last_status_at may be an ISO string from to_dict() — normalize to float epoch
        if isinstance(data.get("last_status_at"), str):
            data["last_status_at"] = _parse_absolute_timestamp(data["last_status_at"])
        # Every non-field key rides in ``extra`` (to_dict writes them all back), so metadata a plugin
        # stores on its own rows survives load -> save -> load. ``_EXTRA_KEYS`` stays the attribute
        # surface for core logic; unknown keys are opaque payload. ``provider`` is the row's owner
        # (excluded from ``field_names`` above), never metadata — sweeping it in would write a
        # stray provider name back over the row on to_dict().
        data["extra"] = {
            k: v for k, v in payload.items() if k not in field_names and k != "provider" and v is not None
        }
        data.setdefault("id", uuid.uuid4().hex[:6])
        data.setdefault("label", payload.get("source", provider))
        data.setdefault("auth_type", AUTH_TYPE_API_KEY)
        data.setdefault("priority", 0)
        data.setdefault("source", SOURCE_MANUAL)
        data.setdefault("access_token", "")
        return cls(provider=provider, **data)

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for field_def in fields(self):
            if field_def.name in {"provider", "extra"}:
                continue
            value = getattr(self, field_def.name)
            if value is not None or field_def.name in _CLEAR_STATUS:
                result[field_def.name] = value
        for k, v in self.extra.items():
            if v is not None:
                result[k] = v
        return sanitize_borrowed_credential_payload(result, self.provider)

    @property
    def runtime_api_key(self) -> str:
        if self.provider == "nous":
            # Nous stores the runtime inference credential in agent_key for
            # compatibility. It must be a NAS invoke JWT.
            for token, expires_at in (
                (self.agent_key, self.agent_key_expires_at),
                (self.access_token, self.expires_at),
            ):
                if (
                    isinstance(token, str)
                    and token.strip()
                    and auth_mod._nous_invoke_jwt_is_usable(
                        token, scope=getattr(self, "scope", None), expires_at=expires_at,
                    )
                ):
                    return token.strip()
            return ""
        return str(self.access_token or "")

    @property
    def runtime_base_url(self) -> Optional[str]:
        if self.provider == "nous":
            return self.inference_base_url or self.base_url
        if self.provider == "openai-codex":
            # Pool rows keep the canonical ChatGPT URL; the profile-scoped proxy override must win
            # for every reader of the row — initial resolution AND a 401/429 rotation
            # (client_lifecycle._swap_credential), or a rotation silently leaves the proxy.
            return get_secret_str("HERMES_CODEX_BASE_URL", "").strip().rstrip("/") or self.base_url
        return self.base_url
