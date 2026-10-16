"""Tests for the memory-audit skill's audit script.

Uses only stdlib + pytest + unittest.mock — no live network calls,
no agent imports. Tests exercise the pure audit functions against
temporary directory trees.
"""

import datetime as _dt
import os
import sys
from pathlib import Path
from unittest import mock

import pytest

# Import the audit module by path (it lives in the skill directory, not
# on the Python path).  We add the script directory to sys.path.
_SKILL_SCRIPT = (
    Path(__file__).resolve().parents[2]
    / "skills"
    / "productivity"
    / "memory-audit"
    / "scripts"
)
sys.path.insert(0, str(_SKILL_SCRIPT))

import memory_audit


# ── Helpers ─────────────────────────────────────────────────────────────


def _write_memory(memory_dir: Path, memory_text: str = "", user_text: str = ""):
    """Write MEMORY.md / USER.md in the given directory."""
    memory_dir.mkdir(parents=True, exist_ok=True)
    (memory_dir / "MEMORY.md").write_text(memory_text, encoding="utf-8")
    (memory_dir / "USER.md").write_text(user_text, encoding="utf-8")


def _entry(*lines: str) -> str:
    """Join lines into a §-delimited entry block."""
    return "\n§\n".join(lines)


# ── audit_hermes_memory ─────────────────────────────────────────────────


class TestAuditHermesMemory:
    def test_clean_memory_no_findings(self, tmp_path):
        _write_memory(
            tmp_path,
            memory_text="Project uses Python 3.11 and pytest.",
            user_text="Prefers concise responses.",
        )
        findings = memory_audit.audit_hermes_memory(tmp_path)
        assert findings == []

    def test_empty_dir_no_findings(self, tmp_path):
        _write_memory(tmp_path, "", "")
        findings = memory_audit.audit_hermes_memory(tmp_path)
        assert findings == []

    def test_missing_dir_no_findings(self, tmp_path):
        # Non-existent directory — not an error, just nothing to check
        findings = memory_audit.audit_hermes_memory(tmp_path / "nonexistent")
        assert findings == []

    def test_detects_api_key_secret(self, tmp_path):
        _write_memory(
            tmp_path,
            memory_text=_entry(
                "Project uses Python 3.11.",
                'api_key="sk-abc123def456ghi789jkl012mno345"',
            ),
        )
        findings = memory_audit.audit_hermes_memory(tmp_path)
        assert any("CRITICAL" in f and "secret" in f.lower() for f in findings)

    def test_detects_bearer_token(self, tmp_path):
        _write_memory(
            tmp_path,
            memory_text="Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9",
        )
        findings = memory_audit.audit_hermes_memory(tmp_path)
        assert any("CRITICAL" in f for f in findings)

    def test_detects_github_token_prefix(self, tmp_path):
        _write_memory(
            tmp_path,
            memory_text="gho_abc123def456ghi789jkl012mno345pqr678",
        )
        findings = memory_audit.audit_hermes_memory(tmp_path)
        assert any("CRITICAL" in f for f in findings)

    def test_detects_stale_pr_reference(self, tmp_path):
        _write_memory(
            tmp_path,
            memory_text=_entry("PR #1234 fixed the memory bug", "Normal fact"),
        )
        findings = memory_audit.audit_hermes_memory(tmp_path)
        assert any("STALE" in f for f in findings)

    def test_detects_stale_commit_sha(self, tmp_path):
        _write_memory(
            tmp_path,
            memory_text="The fix is in commit abc1234def5678",
        )
        findings = memory_audit.audit_hermes_memory(tmp_path)
        assert any("STALE" in f for f in findings)

    def test_detects_stale_phase_done(self, tmp_path):
        _write_memory(
            tmp_path,
            memory_text="Phase 3 done — all tests pass",
        )
        findings = memory_audit.audit_hermes_memory(tmp_path)
        assert any("STALE" in f for f in findings)

    def test_usage_warning_near_limit(self, tmp_path):
        # Fill memory to >85% of the limit
        big_entry = "x" * 2000  # 2000 chars, limit 2200 → 91%
        _write_memory(tmp_path, memory_text=big_entry)
        findings = memory_audit.audit_hermes_memory(
            tmp_path, mem_limit=2200, user_limit=1375
        )
        assert any("USAGE" in f and "MEMORY.md" in f for f in findings)

    def test_no_usage_warning_under_limit(self, tmp_path):
        _write_memory(tmp_path, memory_text="short entry")
        findings = memory_audit.audit_hermes_memory(
            tmp_path, mem_limit=2200, user_limit=1375
        )
        assert not any("USAGE" in f for f in findings)


# ── audit_obsidian_vault ───────────────────────────────────────────────


class TestAuditObsidianVault:
    def test_no_vault_no_findings(self):
        findings = memory_audit.audit_obsidian_vault(Path("/nonexistent/path"))
        assert findings == []

    def test_broken_wikilink_detected(self, tmp_path):
        vault = tmp_path / "vault"
        vault.mkdir()
        (vault / "index.md").write_text(
            "Link to [[Missing Note]] here.", encoding="utf-8"
        )
        (vault / "real.md").write_text("This note exists.", encoding="utf-8")
        findings = memory_audit.audit_obsidian_vault(vault)
        assert any("WIKILINK" in f and "Missing Note" in f for f in findings)

    def test_valid_wikilink_no_findings(self, tmp_path):
        vault = tmp_path / "vault"
        vault.mkdir()
        (vault / "index.md").write_text("Link to [[real]] here.", encoding="utf-8")
        (vault / "real.md").write_text("This note exists.", encoding="utf-8")
        findings = memory_audit.audit_obsidian_vault(vault)
        assert not any("WIKILINK" in f for f in findings)

    def test_wikilink_with_alias_resolves(self, tmp_path):
        vault = tmp_path / "vault"
        vault.mkdir()
        (vault / "index.md").write_text(
            "Link to [[real|the real one]].", encoding="utf-8"
        )
        (vault / "real.md").write_text("This note exists.", encoding="utf-8")
        findings = memory_audit.audit_obsidian_vault(vault)
        assert not any("WIKILINK" in f for f in findings)

    def test_wikilink_with_heading_resolves(self, tmp_path):
        vault = tmp_path / "vault"
        vault.mkdir()
        (vault / "index.md").write_text("Link to [[real#section]].", encoding="utf-8")
        (vault / "real.md").write_text("This note exists.", encoding="utf-8")
        findings = memory_audit.audit_obsidian_vault(vault)
        assert not any("WIKILINK" in f for f in findings)

    def test_missing_daily_note_detected(self, tmp_path):
        vault = tmp_path / "vault"
        vault.mkdir()
        (vault / "note.md").write_text("Some content.", encoding="utf-8")
        findings = memory_audit.audit_obsidian_vault(vault)
        assert any("DAILY" in f for f in findings)

    def test_existing_daily_note_no_finding(self, tmp_path):
        vault = tmp_path / "vault"
        vault.mkdir()
        today = _dt.date.today().isoformat()
        (vault / f"{today}.md").write_text("Today's notes.", encoding="utf-8")
        findings = memory_audit.audit_obsidian_vault(vault)
        assert not any("DAILY" in f for f in findings)


# ── main / exit codes ──────────────────────────────────────────────────


class TestMain:
    def test_healthy_exit_zero(self, tmp_path, capsys):
        _write_memory(tmp_path, "Clean memory.", "Clean user.")
        rc = memory_audit.main(["--memory-dir", str(tmp_path)])
        assert rc == 0
        captured = capsys.readouterr()
        assert captured.out == ""  # SILENT when healthy

    def test_issues_exit_one(self, tmp_path, capsys):
        _write_memory(tmp_path, 'api_key="sk-abc123def456ghi789jkl012mno345"')
        rc = memory_audit.main(["--memory-dir", str(tmp_path)])
        assert rc == 1
        captured = capsys.readouterr()
        assert "CRITICAL" in captured.out

    def test_no_args_uses_default_home(self, tmp_path, monkeypatch, capsys):
        # With HERMES_HOME pointed at tmp_path and no memory dir, should
        # be healthy (empty memories dir)
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        (tmp_path / "memories").mkdir()
        rc = memory_audit.main([])
        assert rc == 0
