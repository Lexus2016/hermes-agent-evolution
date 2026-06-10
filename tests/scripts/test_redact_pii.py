"""Tests for scripts.redact_pii."""

import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "redact_pii.py"


def _run(text: str) -> tuple[int, str, str]:
    assert SCRIPT.exists()
    proc = subprocess.run(
        [sys.executable, str(SCRIPT)],
        input=text,
        capture_output=True,
        text=True,
    )
    return proc.returncode, proc.stdout, proc.stderr


class TestRedactPiiUnit:
    @pytest.mark.parametrize(
        "dirty",
        [
            "Contact me at alice@example.com please",
            "Token sk-abcdefghijklmnopqrstuvwxyz here",
            "AWS AKIAIOSFODNN7EXAMPLE",
            "Secret password=aabbccdd112233445566778899aabbccddeeff00112233445566778899aabbccdd",
            "My home is /home/alice/projects and also /Users/bob/x",
            "Internal IP 10.0.0.1 or 172.16.255.3 or 192.168.1.100",
            "Call me at +1-555-123-4567",
        ],
    )
    def test_dirty_returns_blocked(self, dirty: str):
        rc, out, err = _run(dirty)
        assert rc == 1
        assert "BLOCKED" in err
        assert "[REDACTED]" in out

    @pytest.mark.parametrize(
        "clean",
        [
            "Just a normal description of a bug in memory handling.",
            "The agent failed to complete task #42.",
            "Steps to reproduce: 1) open file 2) edit line 3) save",
        ],
    )
    def test_clean_returns_ok(self, clean: str):
        rc, out, err = _run(clean)
        assert rc == 0
        assert "BLOCKED" not in err
        assert out.strip() == clean.strip()

    def test_github_token_detected(self):
        text = "personal token ghp_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx here"
        rc, out, err = _run(text)
        assert rc == 1
        assert "GitHub token" in err

    def test_multiple_hits_counted(self):
        text = "Email bob@corp.io, IP 192.168.1.5, secret SECRET_KEY=aabbccdd112233445566778899aabbccddeeff00112233445566778899aabbccdd"
        rc, out, err = _run(text)
        assert rc == 1
        # All three pattern classes should be reported
        assert "Email" in err
        assert "IPv4" in err
        assert "secret" in err or "Generic" in err

    def test_redacted_output_does_not_leak(self):
        text = "super_secret=deadbeef0123456789abcdef0123456789abcdef"
        rc, out, err = _run(text)
        assert rc == 1
        # The hex literal should not survive intact in stdout
        assert "deadbeef" not in out
        assert "[REDACTED]" in out


class TestRedactPiiViaStdin:
    def test_empty_is_clean(self):
        rc, out, err = _run("")
        assert rc == 0

    def test_newlines_preserved(self):
        text = "line1\nline2 bob@corp.io\nline3"
        rc, out, err = _run(text)
        assert rc == 1
        lines = out.splitlines()
        assert lines[0] == "line1"
        assert "[REDACTED]" in lines[1]
        assert lines[2] == "line3"
