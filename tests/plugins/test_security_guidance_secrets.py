"""Tests for secret detection in the security-guidance plugin (#398).

Covers ``plugins/security-guidance/secrets.py``:
  * regex detection of well-known credential formats (AWS, GitHub, Slack,
    Google, Stripe, npm, PEM private key, JWT, generic assignment),
  * the conservative Shannon-entropy backstop,
  * false-positive sanity (benign code + placeholder/example values),
  * end-to-end wiring through the plugin's warn-mode hook.

Token-shaped fixtures are ASSEMBLED FROM PARTS at runtime so neither the
repo's secret scanners (GitGuardian on the PR) nor the I/O redactor sees a
contiguous credential in this file. The detector runs on the concatenated
runtime value, so detection still exercises the real regexes.
"""

import importlib.util
import sys
import types
from pathlib import Path

import pytest


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _load_secrets():
    """Import secrets.py in isolation (stdlib-only, no plugin glue)."""
    path = _repo_root() / "plugins" / "security-guidance" / "secrets.py"
    spec = importlib.util.spec_from_file_location(
        "security_guidance_secrets_under_test", path
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _load_plugin_init():
    """Import the plugin __init__.py with patterns.py + secrets.py as siblings."""
    plugin_dir = _repo_root() / "plugins" / "security-guidance"
    if "hermes_plugins" not in sys.modules:
        ns = types.ModuleType("hermes_plugins")
        ns.__path__ = []
        sys.modules["hermes_plugins"] = ns
    spec = importlib.util.spec_from_file_location(
        "hermes_plugins.security_guidance",
        plugin_dir / "__init__.py",
        submodule_search_locations=[str(plugin_dir)],
    )
    mod = importlib.util.module_from_spec(spec)
    mod.__package__ = "hermes_plugins.security_guidance"
    mod.__path__ = [str(plugin_dir)]
    sys.modules["hermes_plugins.security_guidance"] = mod
    spec.loader.exec_module(mod)
    return mod


# Assembled fake credentials (split so secret scanners don't match the file).
_AWS_KEY = "AKIA" + "QKZ7X2MNOP3RTUV9"                       # AKIA + 16 upper/digits
_GH_TOKEN = "ghp" + "_" + ("b" * 36)                         # gh?_ + 36 alnum
_SLACK = "xoxb" + "-" + "123456789012" + "-" + "abcdefghijkl"
_GOOGLE = "AIza" + "Sy" + ("C" * 33)                         # AIza + 35
_STRIPE = "sk" + "_live_" + ("9" * 24)
_NPM = "npm" + "_" + ("a" * 36)
_PEM = "-----BEGIN " + "RSA PRIVATE KEY-----"
_JWT = "eyJ" + ("hbGciOiJIUzI1NiJ9") + "." + "eyJ" + ("zdWIiOiIxMjM0NTY3ODkwIn0") + "." + ("SflKxwRJ_signature_part")
_HIGH_ENTROPY = "kJ8x2Qm9Zp4Lw7Nv1Rb6Tc3Yd5Fg0Hh"           # 32 mixed chars


class TestRegexSecretDetection:
    def setup_method(self):
        self.s = _load_secrets()

    def _names(self, content):
        return {name for name, _ in self.s.scan_secrets("f.py", content)}

    def test_aws_access_key_detected(self):
        assert "aws_access_key_id" in self._names(f'key = "{_AWS_KEY}"\n')

    def test_pem_private_key_detected(self):
        assert "private_key_pem" in self._names(_PEM + "\nMIIE...\n")

    def test_slack_token_detected(self):
        assert "slack_token" in self._names(f'tok = "{_SLACK}"\n')

    def test_github_token_detected(self):
        assert "github_token" in self._names(f'gh = "{_GH_TOKEN}"\n')

    def test_google_api_key_detected(self):
        assert "google_api_key" in self._names(f'g = "{_GOOGLE}"\n')

    def test_stripe_key_detected(self):
        assert "stripe_secret_key" in self._names(f'sk = "{_STRIPE}"\n')

    def test_npm_token_detected(self):
        assert "npm_token" in self._names(f'n = "{_NPM}"\n')

    def test_jwt_detected(self):
        assert "jwt_token" in self._names(f'jwt = "{_JWT}"\n')

    def test_generic_api_key_assignment_detected(self):
        names = self._names('api_key = "' + ("Z" * 24) + '"\n')
        assert "generic_secret_assignment" in names

    def test_prefix_key_with_filler_substring_still_detected(self):
        # A real fixed-prefix key that happens to contain "00000000" must NOT be
        # suppressed — placeholder exclusion for prefix rules is EXAMPLE-only,
        # so a real secret is never silently dropped (nit #1, fail-open fix).
        tok = "ghp" + "_" + "00000000" + ("c" * 28)  # 36 chars after ghp_
        assert "github_token" in self._names(f'gh = "{tok}"\n')

    def test_each_rule_fires_once(self):
        content = f'a = "{_AWS_KEY}"\nb = "{_AWS_KEY}"\n'
        findings = self.s.scan_secrets("f.py", content)
        assert sum(1 for n, _ in findings if n == "aws_access_key_id") == 1


class TestEntropyBackstop:
    def setup_method(self):
        self.s = _load_secrets()

    def test_high_entropy_secret_assignment_flagged(self):
        # 'db_credential' is in the entropy keyword set but is NOT a known-format
        # rule, so only the entropy backstop can catch this random value.
        names = {n for n, _ in self.s.scan_secrets("f.py", f'db_credential = "{_HIGH_ENTROPY}"\n')}
        assert "high_entropy_secret" in names

    def test_low_entropy_secret_named_value_not_flagged(self):
        # Long but low-entropy (repetitive) value assigned to a secret key.
        names = {n for n, _ in self.s.scan_secrets("f.py", 'password = "aaaaaaaaaaaaaaaaaaaaaaaa"\n')}
        assert "high_entropy_secret" not in names

    def test_shannon_entropy_sanity(self):
        assert self.s.shannon_entropy("") == 0.0
        assert self.s.shannon_entropy("aaaaaaaa") < 1.0
        assert self.s.shannon_entropy(_HIGH_ENTROPY) > 4.0

    def test_entropy_skipped_when_known_secret_already_found(self):
        # AWS regex fires -> entropy backstop suppressed (no duplicate noise).
        names = {n for n, _ in self.s.scan_secrets("f.py", f'secret = "{_AWS_KEY}"\n')}
        assert "high_entropy_secret" not in names


class TestFalsePositiveSanity:
    def setup_method(self):
        self.s = _load_secrets()

    def test_benign_code_no_findings(self):
        content = "def add(a, b):\n    return a + b\n\nAPI_TIMEOUT = 30\n"
        assert self.s.scan_secrets("f.py", content) == []

    def test_placeholder_api_key_not_flagged(self):
        assert self.s.scan_secrets("f.py", 'api_key = "your-api-key-here"\n') == []

    def test_example_value_not_flagged(self):
        assert self.s.scan_secrets("f.py", 'token = "EXAMPLE_TOKEN_VALUE_1234567890"\n') == []

    def test_empty_content_no_findings(self):
        assert self.s.scan_secrets("f.py", "") == []

    def test_huge_content_skipped(self):
        big = "x = 1\n" * 60000  # > 256 KB
        assert self.s.scan_secrets("f.py", big) == []


class TestHookIntegration:
    def test_write_file_with_aws_key_warns(self, monkeypatch):
        monkeypatch.delenv("SECURITY_GUIDANCE_BLOCK", raising=False)
        monkeypatch.delenv("SECURITY_GUIDANCE_DISABLE", raising=False)
        mod = _load_plugin_init()
        args = {"path": "/tmp/config.py", "content": f'AWS = "{_AWS_KEY}"\n'}
        result = mod._on_transform_tool_result(
            tool_name="write_file",
            args=args,
            result='{"success": true, "bytes_written": 40}',
        )
        assert isinstance(result, str)
        assert "Security guidance" in result
        assert "credential" in result.lower()

    def test_clean_write_no_warning(self, monkeypatch):
        monkeypatch.delenv("SECURITY_GUIDANCE_BLOCK", raising=False)
        monkeypatch.delenv("SECURITY_GUIDANCE_DISABLE", raising=False)
        mod = _load_plugin_init()
        args = {"path": "/tmp/ok.py", "content": "x = 1\n"}
        assert mod._on_transform_tool_result(
            tool_name="write_file", args=args, result='{"success": true}'
        ) is None

    def test_block_mode_refuses_write_with_secret(self, monkeypatch):
        monkeypatch.setenv("SECURITY_GUIDANCE_BLOCK", "1")
        monkeypatch.delenv("SECURITY_GUIDANCE_DISABLE", raising=False)
        mod = _load_plugin_init()
        args = {"path": "/tmp/config.py", "content": f'GH = "{_GH_TOKEN}"\n'}
        out = mod._on_pre_tool_call(tool_name="write_file", args=args)
        assert isinstance(out, dict) and out.get("action") == "block"
