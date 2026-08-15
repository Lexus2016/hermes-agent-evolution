"""Tests for credential masking of terminal/tool output (#2435)."""

from __future__ import annotations

from tools import credential_masking


def test_masking_disabled_by_default_passthrough():
    credential_masking.reset_cache()
    # The config gate is cache-backed; forcing it off verifies passthrough.
    credential_masking._config_cache["enabled"] = False
    original = "sk-abc123 no change here"
    assert credential_masking.mask_tool_output(original) == original
    credential_masking.reset_cache()


def test_mask_credentials_redacts_known_prefix():
    credential_masking._config_cache["enabled"] = True
    out = credential_masking.mask_credentials("my key is sk-S4v4g3K3y0123456789")
    assert "sk-S4v4g3K3y0123456789" not in out
    credential_masking.reset_cache()


def test_mask_credentials_no_secret_unchanged():
    credential_masking._config_cache["enabled"] = True
    text = "just some normal output with nothing secret"
    assert credential_masking.mask_credentials(text) == text
    credential_masking.reset_cache()


def test_mask_tool_output_string_passthrough_when_off():
    credential_masking._config_cache["enabled"] = False
    value = "sk-letthispassthrough0000000"
    assert credential_masking.mask_tool_output(value) == value
    credential_masking.reset_cache()


def test_mask_tool_output_dict_recurses():
    credential_masking._config_cache["enabled"] = True
    value = {"ok": "fine", "leak": "token ghp_ShouldBeMasked00000000"}
    out = credential_masking.mask_tool_output(value)
    assert isinstance(out, dict)
    assert out["ok"] == "fine"
    assert "ghp_ShouldBeMasked00000000" not in out["leak"]
    credential_masking.reset_cache()


def test_mask_tool_output_non_text_passthrough():
    credential_masking._config_cache["enabled"] = True
    assert credential_masking.mask_tool_output(42) == 42
    assert credential_masking.mask_tool_output(None) is None
    credential_masking.reset_cache()


def test_mask_credentials_never_raises_on_garbage():
    credential_masking._config_cache["enabled"] = True
    # mask_credentials is a total function: non-string inputs return as-is
    # (mask_tool_output is the Any-typed entry point; mask_credentials guards
    # non-str defensively too).
    from typing import Any

    fake: Any = 123
    assert credential_masking.mask_credentials(fake) == 123
    fake_none: Any = None
    assert credential_masking.mask_credentials(fake_none) is None
    credential_masking.reset_cache()
