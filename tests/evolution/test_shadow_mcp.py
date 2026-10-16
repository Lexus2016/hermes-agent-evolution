"""Tests for shadow-MCP outbound endpoint governance (issue #90, rework r2).

The rework (post PR #3056 review) made the matcher bypass-proof. These tests
prove the six bypass classes the review flagged — trailing-dot FQDNs, userinfo,
scheme variants, domain-crossing over-match, port, and IPv6 — evaluate
correctly for BOTH allow and deny policies, plus redaction of query
strings/userinfo and the terminal-denial exception.
"""

from __future__ import annotations

import json

import pytest

from evolution.lib.shadow_mcp import (
    ALERT,
    ALLOW,
    DENY,
    EndpointContact,
    ShadowMcpDeniedError,
    ShadowMcpGovernor,
    ShadowMcpPolicy,
    endpoint_host,
    endpoint_matches,
    redact_url,
)


# --- endpoint normalization ------------------------------------------------

def test_endpoint_host_normalizes_scheme_userinfo_port_dot():
    assert endpoint_host("https://api.example.com:443/v1/x") == "api.example.com"
    assert endpoint_host("http://EXAMPLE.com") == "example.com"
    assert endpoint_host("https://user:pass@example.com:8443/x") == "example.com"
    assert endpoint_host("https://example.com./x") == "example.com"
    assert endpoint_host("bare.host.name") == "bare.host.name"
    assert endpoint_host("") == ""


# --- matcher: the six bypass classes (allow AND deny) ----------------------

def test_matcher_trailing_dot_fqdn():
    # deny 'evil.com' MUST block 'https://evil.com./x' (trailing dot).
    assert endpoint_matches("evil.com", "https://evil.com./x")
    assert endpoint_matches("evil.com", "https://evil.com/x")
    # allow 'good.com' MUST NOT match 'good.com.' (a different host) — here the
    # trailing dot normalizes away, so it DOES match; the point is it matches
    # the intended host, never a sibling.
    assert endpoint_matches("good.com", "https://good.com./x")


def test_matcher_userinfo_stripped():
    # deny 'evil.com' MUST block 'https://user@evil.com/x'.
    assert endpoint_matches("evil.com", "https://user@evil.com/x")
    assert endpoint_matches("evil.com", "https://user:pass@evil.com/x")
    # allow 'good.com' matches despite userinfo.
    assert endpoint_matches("good.com", "https://user@good.com/x")
    assert not endpoint_matches("good.com", "https://user@attacker.net/x")


def test_matcher_scheme_variant():
    # A full-URL deny entry 'https://evil.com' MUST also block 'http://evil.com/x'.
    assert endpoint_matches("https://evil.com", "http://evil.com/x")
    assert endpoint_matches("https://evil.com", "https://evil.com/x")
    # allow entry matches regardless of scheme.
    assert endpoint_matches("http://good.com", "https://good.com/x")


def test_matcher_no_domain_crossing():
    # allow 'https://good.com' MUST NOT match 'https://good.com.attacker.net/x'.
    assert not endpoint_matches("https://good.com", "https://good.com.attacker.net/x")
    assert not endpoint_matches("good.com", "https://good.com.attacker.net/x")
    # and the true subdomain is still excluded unless wildcarded.
    assert not endpoint_matches("good.com", "https://evilgood.com/x")


def test_matcher_port_insensitive():
    # deny 'evil.com' MUST block any port.
    assert endpoint_matches("evil.com", "https://evil.com:8443/x")
    assert endpoint_matches("evil.com:8443", "https://evil.com:443/x")
    assert endpoint_matches("evil.com", "https://evil.com/x")


def test_matcher_ipv6():
    # deny '::1' (or bracketed '[::1]') MUST block a bracketed IPv6 endpoint.
    assert endpoint_matches("::1", "https://[::1]:8080/x")
    assert endpoint_matches("[::1]", "https://[::1]/x")
    assert not endpoint_matches("::1", "https://[::2]/x")
    assert not endpoint_matches("::2", "https://[::1]/x")


def test_matcher_wildcard():
    assert endpoint_matches("*.example.com", "https://api.example.com/x")
    assert endpoint_matches("*.example.com", "https://example.com/x")
    assert endpoint_matches("*.example.com", "https://www.deep.example.com/x")
    assert not endpoint_matches("*.example.com", "https://evil.com/x")
    assert not endpoint_matches("*.example.com", "https://example.com.attacker.net/x")


def test_matcher_exact_host_no_url_prefix_overmatch():
    # Full-URL entries match on hostname only, never str.startswith on the URL.
    assert endpoint_matches("https://api.example.com/v1", "https://api.example.com/v2/x")
    assert not endpoint_matches("https://api.example.com/v1", "https://api.example.net/v1/x")


def test_matcher_empty_and_garbage():
    assert not endpoint_matches("", "https://example.com")
    assert not endpoint_matches("example.com", "")
    assert not endpoint_matches("*", "https://example.com")
    assert not endpoint_matches("*.", "https://example.com")


# --- redaction -------------------------------------------------------------

def test_redact_url_strips_query_userinfo_fragment():
    assert redact_url("https://user:pass@a.com/path?token=SECRET#frag") == "https://a.com/path"
    assert redact_url("https://a.com/x?api_key=abc") == "https://a.com/x"
    assert redact_url("https://a.com/x") == "https://a.com/x"
    assert redact_url("https://a.com:8443/x?q=1") == "https://a.com:8443/x"
    assert redact_url("bare.host") == "bare.host"
    assert redact_url("") == ""


# --- policy ----------------------------------------------------------------

def test_policy_default_is_allow_all():
    assert ShadowMcpPolicy().evaluate("https://anything.example.com/x") == ALLOW


def test_policy_allowlist_alerts_unapproved():
    policy = ShadowMcpPolicy(allow=["approved.com"])
    assert policy.evaluate("https://approved.com/x") == ALLOW
    assert policy.evaluate("https://other.com/x") == ALERT


def test_policy_denylist_blocks_bypass_variants():
    policy = ShadowMcpPolicy(deny=["evil.com"])
    assert policy.evaluate("https://evil.com./x") == DENY  # trailing dot
    assert policy.evaluate("https://user@evil.com/x") == DENY  # userinfo
    assert policy.evaluate("http://evil.com:8443/x") == DENY  # scheme + port
    assert policy.evaluate("https://good.com/x") == ALLOW


def test_policy_deny_wins_over_allow():
    policy = ShadowMcpPolicy(allow=["example.com"], deny=["example.com"])
    assert policy.evaluate("https://example.com/x") == DENY


def test_policy_from_config():
    policy = ShadowMcpPolicy.from_config(
        {"enabled": False, "allow": ["a.com"], "deny": ["b.com"], "junk": [1, 2]}
    )
    assert policy.enabled is False
    assert policy.allow == ["a.com"]
    assert policy.deny == ["b.com"]


def test_policy_from_config_none_or_garbage():
    assert ShadowMcpPolicy.from_config(None).enabled is True
    assert ShadowMcpPolicy.from_config("nope").allow == []


# --- governor --------------------------------------------------------------

def test_governor_records_and_counts(tmp_path):
    gov = ShadowMcpGovernor(policy=ShadowMcpPolicy())
    assert gov.record_contact("srv", "https://a.com/x") == ALLOW
    assert gov.record_contact("srv", "https://a.com/x") == ALLOW
    assert gov.record_contact("srv", "https://b.com/x") == ALLOW

    digest = gov.audit_digest()
    assert len(digest) == 2
    by_endpoint = {d["endpoint"]: d for d in digest}
    assert by_endpoint["https://a.com/x"]["count"] == 2
    assert by_endpoint["https://b.com/x"]["count"] == 1
    assert gov.unapproved() == []


def test_governor_redacts_endpoints_in_memory_and_on_disk(tmp_path):
    log_path = tmp_path / "shadow.jsonl"
    gov = ShadowMcpGovernor(policy=ShadowMcpPolicy(), log_path=log_path)
    gov.record_contact("srv", "https://user:pass@a.com/path?token=SECRET#frag")

    # In-memory digest must be redacted (no secret).
    digest = gov.audit_digest()
    assert digest[0]["endpoint"] == "https://a.com/path"
    assert "SECRET" not in json.dumps(digest)

    # Disk JSONL must be redacted too.
    line = log_path.read_text(encoding="utf-8").strip()
    assert "SECRET" not in line
    assert "user:pass" not in line
    assert "token=" not in line


def test_governor_alerts_unapproved():
    gov = ShadowMcpGovernor(policy=ShadowMcpPolicy(allow=["ok.com"]))
    assert gov.record_contact("srv", "https://ok.com/x") == ALLOW
    assert gov.record_contact("srv", "https://unapproved.com/x") == ALERT
    assert [d["endpoint"] for d in gov.unapproved()] == ["https://unapproved.com/x"]


def test_governor_deny_verdict():
    gov = ShadowMcpGovernor(policy=ShadowMcpPolicy(deny=["bad.com"]))
    assert gov.record_contact("srv", "https://bad.com/x") == DENY
    assert gov.unapproved()[0]["last_verdict"] == DENY


def test_governor_disabled_policy_never_blocks_or_alerts():
    gov = ShadowMcpGovernor(policy=ShadowMcpPolicy(allow=["ok.com"], enabled=False))
    assert gov.record_contact("srv", "https://whatever.com/x") == ALLOW
    assert gov.unapproved() == []


def test_governor_writes_jsonl(tmp_path):
    log_path = tmp_path / "shadow.jsonl"
    gov = ShadowMcpGovernor(policy=ShadowMcpPolicy(), log_path=log_path)
    gov.record_contact("srv", "https://a.com/x")
    gov.record_contact("srv", "https://a.com/x")

    lines = log_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    rec = json.loads(lines[0])
    assert rec["server"] == "srv"
    assert rec["endpoint"] == "https://a.com/x"
    assert rec["host"] == "a.com"
    assert rec["count"] == 1


def test_governor_alerter_called_on_alert():
    seen = []

    def _alerter(verdict, contact):
        seen.append((verdict, contact.endpoint))

    gov = ShadowMcpGovernor(policy=ShadowMcpPolicy(allow=["ok.com"]), alerter=_alerter)
    gov.record_contact("srv", "https://ok.com/x")
    gov.record_contact("srv", "https://nope.com/x")
    assert seen == [(ALERT, "https://nope.com/x")]


def test_governor_clear_drops_contacts():
    gov = ShadowMcpGovernor()
    gov.record_contact("srv", "https://a.com/x")
    gov.clear()
    assert gov.audit_digest() == []


def test_endpoint_contact_roundtrip():
    c = EndpointContact(server="srv", endpoint="https://a.com/x", count=3)
    d = c.to_dict()
    assert EndpointContact.from_dict(d).to_dict() == d


def test_denied_error_is_distinct_and_connection_error():
    err = ShadowMcpDeniedError("blocked")
    assert isinstance(err, ConnectionError)
    # Distinct from the generic ConnectionError so the reconnect loop can
    # classify it as permanent.
    assert type(err) is ShadowMcpDeniedError


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
