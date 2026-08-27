"""Tests for scripts.redact_pii."""

import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "redact_pii.py"

# Secret-shaped fixtures are assembled at runtime so secret-scanning bots
# (GitGuardian etc.) don't flag the literals in the diff as real leaks.
# The concatenated results still match redact_pii's detection regexes.
FAKE_SK_TOKEN = "sk-" + "abcdefghijklmnopqrstuvwxyz"
FAKE_AWS_KEY = "AKIA" + "IOSFODNN7EXAMPLE"  # AWS docs example key id
FAKE_HEX_BLOB = (
    "aabbccdd112233445566778899" + "aabbccddeeff00112233445566778899aabbccdd"
)
FAKE_GHP_TOKEN = "ghp_" + "x" * 36
FAKE_HEX_SHORT = "deadbeef" + "0123456789abcdef" * 2


def _run(text: str, extra_args: list[str] | None = None) -> tuple[int, str, str]:
    assert SCRIPT.exists()
    cmd = [sys.executable, str(SCRIPT), *(extra_args or [])]
    proc = subprocess.run(
        cmd,
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
            f"Token {FAKE_SK_TOKEN} here",
            f"AWS {FAKE_AWS_KEY}",
            f"Secret password={FAKE_HEX_BLOB}",
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
        text = f"personal token {FAKE_GHP_TOKEN} here"
        rc, out, err = _run(text)
        assert rc == 1
        assert "GitHub token" in err

    def test_multiple_hits_counted(self):
        text = f"Email bob@corp.io, IP 192.168.1.5, secret SECRET_KEY={FAKE_HEX_BLOB}"
        rc, out, err = _run(text)
        assert rc == 1
        # All three pattern classes should be reported
        assert "Email" in err
        assert "IPv4" in err
        assert "secret" in err or "Generic" in err

    def test_redacted_output_does_not_leak(self):
        text = f"super_secret={FAKE_HEX_SHORT}"
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


class TestRedactPiiQuarantine:
    def test_blocked_body_is_quarantined(self, tmp_path: Path):
        qdir = tmp_path / "quarantine"
        slug = "proposal-test-feature"
        body = (
            "Contact me at alice@example.com, my token is sk-"
            + "x" * 32
            + ", and my home is /home/bob/projects."
        )
        rc, out, err = _run(
            body, extra_args=["--quarantine-dir", str(qdir), "--slug", slug]
        )
        assert rc == 1
        # stdout is the redacted body
        assert "alice@example.com" not in out
        assert "[REDACTED]" in out
        # quarantine file was created
        files = list(qdir.iterdir())
        assert len(files) == 1
        qfile = files[0]
        assert qfile.suffix == ".md"
        assert slug in qfile.name
        content = qfile.read_text(encoding="utf-8")
        assert "# Quarantined evolution issue body" in content
        # Reasons are recorded in the file (non-empty).
        assert "redaction_reasons:" in content
        # Raw secrets and home path do not leak into the quarantine file either.
        assert "alice@example.com" not in content
        assert "/home/bob/projects" not in content
        assert "sk-" + "x" * 32 not in content
        # stderr carries structured metadata for logging.
        assert "quarantine_path" in err
        assert qfile.name in err

    def test_clean_body_does_not_create_quarantine(self, tmp_path: Path):
        qdir = tmp_path / "quarantine"
        rc, out, err = _run(
            "A plain description of a memory handling bug.",
            extra_args=["--quarantine-dir", str(qdir), "--slug", "clean"],
        )
        assert rc == 0
        # Clean bodies never create the quarantine dir — assert non-existence,
        # not "dir exists but is empty" (iterdir() would raise FileNotFoundError).
        assert not qdir.exists()
        assert "quarantine_path" not in err

    def test_missing_slug_uses_default(self, tmp_path: Path):
        qdir = tmp_path / "quarantine"
        rc, out, err = _run(
            "Internal IP 192.168.1.1",
            extra_args=["--quarantine-dir", str(qdir)],
        )
        assert rc == 1
        files = list(qdir.iterdir())
        assert len(files) == 1
        assert "blocked" in files[0].name

    def test_quarantine_covers_acceptance_patterns(self, tmp_path: Path):
        """Regression for the PII gate acceptance criteria (#3236)."""
        qdir = tmp_path / "quarantine"
        samples = [
            ("email@example.com", "Email"),
            ("/home/alice/config.yaml", "Absolute home path"),
            ("server at 10.0.0.1", "Private IPv4"),
            ("github token gho_" + "1" * 36, "GitHub token"),
            ("secretKey=" + "a" * 48, "Generic secret"),
        ]
        for sample, expected_reason in samples:
            rc, out, err = _run(
                sample,
                extra_args=["--quarantine-dir", str(qdir), "--slug", "pattern"],
            )
            assert rc == 1, f"{sample} should be blocked"
            files = list(qdir.iterdir())
            assert files, f"{sample} should be quarantined"
            content = files[-1].read_text(encoding="utf-8")
            assert expected_reason in content, f"{sample}: expected {expected_reason}"
            # raw sample must not survive into the quarantine file
            assert sample not in content
            # remove the file for the next sample to keep directory clean
            files[-1].unlink()
