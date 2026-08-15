"""Tests for tools.credential_masking — issue #2435.

Covers: every pattern family, idempotency, empty/None handling,
count/has helpers, the config gate default-off behaviour, and the
mask_tool_output choke point (passthrough when off, masked when on).
"""

import sys
import types

import pytest

from tools.credential_masking import (
    CREDENTIAL_PATTERNS,
    count_credentials,
    has_credentials,
    mask_credentials,
    mask_tool_output,
    masking_enabled,
)


# ── Pattern families ────────────────────────────────────────────────────


class TestPatternFamilies:
    def test_aws_access_key_id(self):
        raw = "export AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE ok"
        out = mask_credentials(raw)
        assert "AKIAIOSFODNN7EXAMPLE" not in out
        assert "[REDACTED:AWS-KEY]" in out
        assert "AWS_ACCESS_KEY_ID=" in out

    def test_github_token(self):
        raw = "token ghp_" + "a" * 36
        out = mask_credentials(raw)
        assert "ghp_" not in out
        assert "[REDACTED:GITHUB-TOKEN]" in out

    def test_openai_style_key(self):
        raw = "Authorization: sk-" + "b" * 24
        out = mask_credentials(raw)
        assert "sk-" not in out.replace("sk-ant-", "")
        assert "[REDACTED:OPENAI-KEY]" in out or "[REDACTED:BEARER-TOKEN]" in out

    def test_anthropic_key_before_generic(self):
        raw = "key sk-ant-" + "c" * 20
        out = mask_credentials(raw)
        assert "[REDACTED:ANTHROPIC-KEY]" in out

    def test_slack_token(self):
        raw = "SLACK_TOKEN=xoxb-" + "1" * 20 + "-abc"
        out = mask_credentials(raw)
        assert "xoxb-" not in out
        assert "[REDACTED:SLACK-TOKEN]" in out

    def test_jwt(self):
        raw = "jwt=eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
        out = mask_credentials(raw)
        assert "eyJhbGci" not in out
        assert "[REDACTED:JWT]" in out

    def test_private_key_block(self):
        raw = (
            "-----BEGIN RSA PRIVATE KEY-----\nMIIEpAIBAAKCAQEA7\n"
            "-----END RSA PRIVATE KEY-----\ntrailing"
        )
        out = mask_credentials(raw)
        assert "MIIEpAIBAAKCAQEA7" not in out
        assert "[REDACTED:PRIVATE-KEY]" in out
        assert "trailing" in out

    def test_url_password_redacted_user_kept(self):
        raw = "postgres://alice:s3cret-pw@db.example.com:5432/prod"
        out = mask_credentials(raw)
        assert "s3cret-pw" not in out
        assert "alice" in out
        assert "db.example.com" in out
        assert "[REDACTED:URL-PASSWORD]" in out

    def test_bearer_header(self):
        raw = "Authorization: Bearer abcdefghijklmnopqrstuvwxyz1234"
        out = mask_credentials(raw)
        assert "abcdefghijklmnopqrstuvwxyz1234" not in out
        assert "Bearer [REDACTED:BEARER-TOKEN]" in out

    def test_stripe_live_key(self):
        raw = "sk_live_" + "d" * 24
        out = mask_credentials(raw)
        assert "[REDACTED:STRIPE-KEY]" in out

    def test_google_api_key(self):
        raw = "key=AIza" + "e" * 35
        out = mask_credentials(raw)
        assert "[REDACTED:GOOGLE-KEY]" in out


# ── Non-secrets must survive ────────────────────────────────────────────


class TestNonSecretsPassThrough:
    def test_ordinary_text_untouched(self):
        raw = "The quick brown fox jumps over 12 lazy dogs (2026-08-15)."
        assert mask_credentials(raw) == raw

    def test_short_hex_and_words_not_redacted(self):
        raw = "commit abc1234 deprecated skiff dock worker"
        assert mask_credentials(raw) == raw

    def test_empty_and_none(self):
        assert mask_credentials("") == ""
        assert mask_credentials(None) == ""
        assert mask_tool_output(None) == ""


# ── Idempotency ─────────────────────────────────────────────────────────


class TestIdempotency:
    def test_double_mask_is_noop(self):
        raw = "key=AKIAIOSFODNN7EXAMPLE pw=postgres://u:hunter2@h/db"
        once = mask_credentials(raw)
        twice = mask_credentials(once)
        assert once == twice

    def test_marker_not_token_shaped(self):
        for name, regex, _replacement in CREDENTIAL_PATTERNS:
            marker = f"[REDACTED:{name}]"
            assert not regex.search(marker), name


# ── Count / has ─────────────────────────────────────────────────────────


class TestCountHas:
    def test_count_sums_across_patterns(self):
        raw = "AKIAIOSFODNN7EXAMPLE and ghp_" + "a" * 36
        assert count_credentials(raw) >= 2

    def test_has_credentials(self):
        assert has_credentials("xoxb-" + "9" * 20)
        assert not has_credentials("nothing to see here")
        assert not has_credentials(None)


# ── Config gate ─────────────────────────────────────────────────────────


class _FakeCfgModule(types.ModuleType):
    def __init__(self, cfg):
        super().__init__("hermes_cli.config")
        self._cfg = cfg

    def load_config(self):
        if isinstance(self._cfg, Exception):
            raise self._cfg
        return self._cfg


class TestConfigGate:
    def _install(self, monkeypatch, cfg):
        fake = _FakeCfgModule(cfg)
        monkeypatch.setitem(sys.modules, "hermes_cli", types.ModuleType("hermes_cli"))
        monkeypatch.setitem(sys.modules, "hermes_cli.config", fake)

    def test_default_off_when_config_missing(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "hermes_cli", types.ModuleType("hermes_cli"))
        monkeypatch.setitem(
            sys.modules,
            "hermes_cli.config",
            types.SimpleNamespace(load_config=None),
        )
        # import inside the function raises TypeError on None call → default
        assert masking_enabled() is False

    def test_off_by_default(self, monkeypatch):
        self._install(monkeypatch, {"security": {}})
        assert masking_enabled() is False

    def test_on_when_config_set(self, monkeypatch):
        self._install(monkeypatch, {"security": {"credential_masking": True}})
        assert masking_enabled() is True

    def test_config_error_falls_back_to_default(self, monkeypatch):
        self._install(monkeypatch, RuntimeError("boom"))
        assert masking_enabled() is False

    def test_mask_tool_output_passthrough_when_off(self, monkeypatch):
        self._install(monkeypatch, {"security": {"credential_masking": False}})
        raw = "token AKIAIOSFODNN7EXAMPLE"
        assert mask_tool_output(raw) == raw

    def test_mask_tool_output_masks_when_on(self, monkeypatch):
        self._install(monkeypatch, {"security": {"credential_masking": True}})
        raw = "token AKIAIOSFODNN7EXAMPLE"
        out = mask_tool_output(raw)
        assert "AKIAIOSFODNN7EXAMPLE" not in out
        assert "[REDACTED:AWS-KEY]" in out


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
