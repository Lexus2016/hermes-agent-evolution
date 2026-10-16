"""Shadow-MCP governance — outbound endpoint visibility + approval (#90).

The local counterpart to Cloudflare's 2026-08-14 Gateway ``shadow-MCP``
selector (``experimental.is_mcp``). Enterprise gateways can now see and block
*unapproved* MCP connections at the network layer; the local agent has no such
view of what its MCP/tool traffic actually touches. This module provides that
visibility without a gateway:

1. **Contact log** — every outbound MCP/tool endpoint contact is recorded
   (endpoint, owning server, contact count, first/last-seen).
2. **Config-driven allow/deny** — a ``shadow_mcp`` policy in ``config.yaml``
   declares which endpoints are approved, blocked, or (by default) simply
   observed.
3. **Alert on unapproved contact** — a contact outside the allow list raises
   a warning-level alert; a contact on the deny list is a denial.
4. **Audit digest** — the contact log and its unapproved subset are exposed
   for the audit channel (see ``scripts/shadow_mcp_audit.py``).

Policy (``config.yaml``)::

    shadow_mcp:
      enabled: true          # default true — log every outbound endpoint contact
      allow: []              # empty = allow-all (observe only, never block)
      deny: []               # explicit blocklist (deny verdict)

Verbs returned by :meth:`ShadowMcpGovernor.record_contact`:

* ``allow``      — approved (allow list empty, or endpoint matches it; and not denied).
* ``alert``      — unapproved (allow list non-empty and endpoint not on it). Logged, not blocked.
* ``deny``       — blocked (endpoint on the deny list). The caller should refuse the connection.

The default (empty allow/deny) is **fail-open**: it observes everything and
blocks nothing, so enabling the feature never breaks existing MCP servers.
Tightening to ``allow: [ ... ]`` opts into alerting; ``deny: [ ... ]`` opts
into blocking.

Matching semantics (security-relevant, reworked after PR #3056 review): every
entry — bare host, ``*.wildcard``, or full URL — is compared on the
``urlparse().hostname`` of both sides. Scheme, userinfo, port, and trailing
dot are all normalized away BEFORE comparison, so a deny entry for
``evil.com`` also blocks ``https://user@evil.com./x`` and
``http://evil.com:8443/y``, and an allow entry for ``good.com`` can never
over-match ``good.com.attacker.net``. Full-URL entries match on hostname
only — there is deliberately **no** ``str.startswith`` on the whole URL.

Secrets are never written to the contact log: query strings and userinfo are
redacted at ingestion (see :func:`redact_url`), so the audit CLI can print
``endpoint`` without leaking tokens.

This module is import-safe and dependency-light (stdlib only) so the MCP
dispatch path can import it without pulling in the agent core.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone, UTC
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import urlparse, urlunparse

logger = logging.getLogger(__name__)

__all__ = [
    "ALERT",
    "ALLOW",
    "DENY",
    "EndpointContact",
    "ShadowMcpDeniedError",
    "ShadowMcpGovernor",
    "ShadowMcpPolicy",
    "default_log_path",
    "endpoint_host",
    "endpoint_matches",
    "get_governor",
    "redact_url",
]

ALLOW = "allow"
ALERT = "alert"
DENY = "deny"


def _now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _normalize_host(value: str) -> str:
    """Normalize a URL or bare host to a comparable, lowercased hostname.

    Strips scheme, userinfo, port, path/query/fragment, surrounding IPv6
    brackets, and any trailing dot. Returns ``""`` for empty/unparseable input.

    This is the single source of truth for endpoint comparison: matching a
    policy entry against an endpoint happens on the *hostname* only, so
    userinfo / port / trailing-dot / scheme variants can never slip past a
    deny entry, and an allow entry can never over-match across a domain
    boundary (both failure modes of the original ``str.startswith`` matcher).
    """
    value = (value or "").strip()
    if not value:
        return ""
    # Prefix a scheme when absent so urlparse treats a bare host / user@host /
    # host:port string as a netloc instead of a (path-like) bare string.
    parsed = urlparse(value if "://" in value else "//" + value)
    host = parsed.hostname or ""
    if host:
        return host.rstrip(".").lower()
    # urlparse() leaves hostname empty for inputs it cannot split — most
    # commonly a BARE IPv6 literal ('::1' without brackets), or a
    # userinfo/host with no scheme. Extract the host manually.
    netloc = (parsed.netloc or value).strip()
    if "@" in netloc:
        netloc = netloc.rsplit("@", 1)[-1]
    if netloc.startswith("[") and "]" in netloc:
        netloc = netloc[1 : netloc.index("]")]
    elif netloc.count(":") > 1:
        # Bare IPv6 literal: the whole netloc is the address (no port).
        pass
    elif ":" in netloc:
        # host:port -> host
        netloc = netloc.split(":", 1)[0]
    return netloc.rstrip(".").lower()


def endpoint_host(endpoint: str) -> str:
    """Return the normalized hostname of a URL (or bare host).

    Strips scheme, userinfo, port, path/query/fragment, and any trailing dot,
    and lowercases. Returns ``""`` for empty input.
    """
    return _normalize_host(endpoint)


def endpoint_matches(entry: str, endpoint: str) -> bool:
    """True when a policy *entry* matches an *endpoint* URL or host.

    Match rules (all on ``urlparse().hostname``, never on the whole URL):

    * ``*.example.com`` — wildcard: matches ``example.com`` and any subdomain
      (``api.example.com``, ``www.deep.example.com``), port and trailing dot
      irrelevant.
    * ``example.com`` / ``https://example.com/x`` — exact hostname match,
      ignoring scheme, userinfo, port, path, query, and trailing dot.
    """
    entry_host = _normalize_host(entry)
    endpoint_host_norm = _normalize_host(endpoint)
    if not entry_host or not endpoint_host_norm:
        return False

    if entry_host.startswith("*."):
        suffix = entry_host[2:]
        if not suffix:
            return False
        return endpoint_host_norm == suffix or endpoint_host_norm.endswith("." + suffix)
    return endpoint_host_norm == entry_host


def redact_url(value: str) -> str:
    """Return *value* with userinfo, query string, and fragment removed.

    Only rewrites when *value* carries a scheme; bare hostnames and non-URL
    strings are returned unchanged. Port and path are preserved so the contact
    log stays useful for triage, but anything that could carry a secret
    (``user:pass@``, ``?token=``, ``#fragment``) is dropped before it reaches
    the JSONL log or the audit digest.
    """
    value = (value or "").strip()
    if not value or "://" not in value:
        return value
    try:
        parsed = urlparse(value)
    except ValueError:
        return value
    host = parsed.hostname or ""
    if not host:
        # Cannot safely rebuild — fall back to dropping query/fragment only.
        return value.split("?", 1)[0].split("#", 1)[0]
    # Rebuild a secrets-safe URL: scheme + host[:port] + path.
    try:
        port = parsed.port
    except ValueError:
        port = None
    if port is not None:
        netloc = f"[{host}]:{port}" if ":" in host else f"{host}:{port}"
    else:
        netloc = host
    return urlunparse((parsed.scheme, netloc, parsed.path or "", "", "", ""))


class ShadowMcpDeniedError(ConnectionError):
    """Raised when an outbound endpoint is blocked by the shadow_mcp deny policy.

    Subclasses :class:`ConnectionError` so callers that only catch the broad
    class still treat it as a connection problem. It is a DISTINCT type so the
    MCP reconnect loop can classify it as *permanent* — a deny verdict never
    clears on retry, so the server task terminates immediately instead of
    parking in the reconnect-backoff ladder (the original PR #3056 failure:
    a denied endpoint warn-spammed through the retry loop).
    """


def default_log_path() -> Optional[Path]:
    """Best-effort default on-disk contact-log path, or ``None`` if unavailable."""
    try:
        from hermes_constants import get_hermes_home

        return get_hermes_home() / "logs" / "shadow_mcp.jsonl"
    except Exception:
        return None


@dataclass
class EndpointContact:
    """Aggregate of contacts to a single (server, endpoint) pair.

    ``endpoint`` is the *redacted* URL (no query string / userinfo / fragment);
    ``host`` is the normalized hostname. Both are safe to print in an audit.
    """

    server: str
    endpoint: str
    host: str = ""
    count: int = 0
    first_seen: str = ""
    last_seen: str = ""
    last_verdict: str = ALLOW

    def __post_init__(self) -> None:
        self.host = self.host or endpoint_host(self.endpoint)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> EndpointContact:
        return cls(
            server=str(d.get("server", "")),
            endpoint=str(d.get("endpoint", "")),
            host=str(d.get("host", "")),
            count=int(d.get("count", 0)),
            first_seen=str(d.get("first_seen", "")),
            last_seen=str(d.get("last_seen", "")),
            last_verdict=str(d.get("last_verdict", ALLOW)),
        )


@dataclass
class ShadowMcpPolicy:
    """Config-driven allow/deny policy for outbound endpoints.

    ``allow`` and ``deny`` are lists of endpoint entries (host, wildcard
    ``*.host``, or full URL). Empty ``allow`` means allow-all (observe-only).
    Entries are matched via :func:`endpoint_matches`.
    """

    allow: list[str] = field(default_factory=list)
    deny: list[str] = field(default_factory=list)
    enabled: bool = True

    @classmethod
    def from_config(cls, config: Optional[dict[str, Any]]) -> ShadowMcpPolicy:
        """Build a policy from a ``shadow_mcp`` config mapping (or ``None``)."""
        if not isinstance(config, dict):
            return cls()
        allow = config.get("allow") or []
        deny = config.get("deny") or []
        enabled = bool(config.get("enabled", True))
        return cls(
            allow=[str(a) for a in allow] if isinstance(allow, (list, tuple)) else [],
            deny=[str(d) for d in deny] if isinstance(deny, (list, tuple)) else [],
            enabled=enabled,
        )

    def evaluate(self, endpoint: str) -> str:
        """Return ``allow`` / ``alert`` / ``deny`` for an endpoint."""
        if any(endpoint_matches(entry, endpoint) for entry in self.deny):
            return DENY
        if not self.allow:
            return ALLOW
        if any(endpoint_matches(entry, endpoint) for entry in self.allow):
            return ALLOW
        return ALERT


class ShadowMcpGovernor:
    """Runtime governor: records contacts, applies policy, alerts, and audits.

    Keeps an in-memory aggregate keyed by ``(server, redacted_endpoint)`` and,
    when ``log_path`` is set, appends each contact as a JSONL line for the
    audit channel. Endpoints are redacted before storage so secrets never
    reach memory or disk. The caller (the MCP dispatch path) is responsible
    for *acting* on a ``deny`` verdict (refusing the connection); this class
    only reports.
    """

    def __init__(
        self,
        policy: Optional[ShadowMcpPolicy] = None,
        log_path: Optional[Path | str] = None,
        alerter: Optional[Callable[[str, EndpointContact], None]] = None,
    ) -> None:
        self.policy = policy or ShadowMcpPolicy()
        self.log_path = Path(log_path) if log_path else None
        self._alerter = alerter
        self._contacts: dict[str, EndpointContact] = {}

    def record_contact(self, server: str, endpoint: str) -> str:
        """Record a contact, return its verdict (``allow``/``alert``/``deny``).

        Always logs the contact when the policy is enabled. Emits a
        warning-level alert (and invokes the optional alerter) when the
        verdict is ``alert`` or ``deny``.
        """
        safe_endpoint = redact_url(endpoint)
        key = f"{server}\x00{safe_endpoint}"
        contact = self._contacts.get(key)
        now = _now_iso()

        verdict = self.policy.evaluate(endpoint) if self.policy.enabled else ALLOW

        if contact is None:
            contact = EndpointContact(
                server=str(server),
                endpoint=safe_endpoint,
                count=1,
                first_seen=now,
                last_seen=now,
                last_verdict=verdict,
            )
            self._contacts[key] = contact
        else:
            contact.count += 1
            contact.last_seen = now
            contact.last_verdict = verdict

        if self.policy.enabled:
            self._append_disk(contact)
            if verdict in (ALERT, DENY):
                self._alert(verdict, contact)
        return verdict

    def _alert(self, verdict: str, contact: EndpointContact) -> None:
        logger.warning(
            "shadow-mcp: %s outbound endpoint contact with %s (%s) via server '%s'",
            verdict,
            contact.endpoint,
            contact.host,
            contact.server,
        )
        if self._alerter is not None:
            try:
                self._alerter(verdict, contact)
            except Exception:  # pragma: no cover - alerting must never break dispatch
                logger.debug("shadow-mcp alerter failed", exc_info=True)

    def _append_disk(self, contact: EndpointContact) -> None:
        path = self.log_path
        if path is None:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(contact.to_dict()) + "\n")
        except OSError as exc:
            logger.debug("shadow-mcp disk append failed: %s", exc)

    def audit_digest(self) -> list[dict[str, Any]]:
        """All recorded contacts, most-recently-seen first."""
        contacts = list(self._contacts.values())
        contacts.sort(key=lambda c: c.last_seen, reverse=True)
        return [c.to_dict() for c in contacts]

    def unapproved(self) -> list[dict[str, Any]]:
        """Contacts whose last verdict was ``alert`` or ``deny``."""
        return [
            c.to_dict()
            for c in self._contacts.values()
            if c.last_verdict in (ALERT, DENY)
        ]

    def clear(self) -> None:
        """Drop all in-memory contacts (session boundary)."""
        self._contacts.clear()


_governor: Optional[ShadowMcpGovernor] = None


def get_governor() -> ShadowMcpGovernor:
    """Return the process-wide governor, lazily built from the active config."""
    global _governor
    if _governor is None:
        policy = ShadowMcpPolicy()
        try:
            from hermes_cli.config import load_config

            policy = ShadowMcpPolicy.from_config(load_config().get("shadow_mcp"))
        except Exception:
            logger.debug("shadow-mcp config load failed; using default policy", exc_info=True)
        _governor = ShadowMcpGovernor(policy=policy, log_path=default_log_path())
    return _governor


# Retained for reference: a caller can reset the singleton (e.g. tests).
def _reset_governor() -> None:
    global _governor
    _governor = None
