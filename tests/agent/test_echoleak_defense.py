"""Tests for EchoLeak + agentjacking defenses (issue #1717)."""

from agent.echoleak_defense import (
    fence_debug_output,
    neutralize_images,
    sanitize_error_for_context,
    sanitize_output,
)


def test_remote_image_link_neutralized():
    out = neutralize_images("![img](https://evil.com/pixel.png)")
    assert "[blocked-image-link]" in out
    assert "evil.com" not in out


def test_data_image_neutralized():
    out = neutralize_images("![x](data:image/png;base64,AAAA)")
    assert "[blocked-data-image]" in out


def test_benign_text_passes_through():
    text = "Here is some plain documentation text."
    assert neutralize_images(text) == text


def test_debug_traceback_fenced_and_secrets_redacted():
    out = fence_debug_output(
        "Traceback (most recent call last):\napi_key=abc123\nLine failed.\n"
    )
    assert out.startswith("[untrusted-debug:")
    assert "abc123" not in out
    assert "api_key=[redacted]" in out


def test_error_string_secrets_redacted():
    err = "Authentication failed: password=supersecret"
    out = sanitize_error_for_context(err)
    assert "supersecret" not in out
    assert "password=[redacted]" in out


def test_sanitize_output_fail_open_and_non_string():
    assert sanitize_output("plain") == "plain"
    assert sanitize_output(42) == 42
    assert sanitize_output("") == ""
