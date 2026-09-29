"""Unit tests for hermes_cli.security_audit — parsers + OSV plumbing.

These never hit the live OSV API; HTTP is monkeypatched. The live-call path
is exercised in the E2E test embedded in PR validation, not here.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch


from hermes_cli import security_audit as sa


# ─── Parsers ──────────────────────────────────────────────────────────────────


class TestRequirementsParser:
    def test_extracts_pinned_versions(self):
        text = "requests==2.20.0\nflask==2.0.1\n"
        assert sa._parse_requirements(text) == [
            ("requests", "2.20.0"),
            ("flask", "2.0.1"),
        ]

    def test_skips_comments_and_options(self):
        text = "# comment\n-r other.txt\n--index-url https://x\nflask==2.0.1\n"
        assert sa._parse_requirements(text) == [("flask", "2.0.1")]




class TestMCPComponentExtraction:
    def test_npx_scoped_pinned(self):
        comp = sa._extract_mcp_component(
            "fs", "npx", ["-y", "@modelcontextprotocol/server-filesystem@0.5.0"]
        )
        assert comp == sa.Component(
            name="@modelcontextprotocol/server-filesystem",
            version="0.5.0",
            ecosystem="npm",
            source="mcp:fs",
        )



    def test_empty_args(self):
        assert sa._extract_mcp_component("x", "npx", []) is None


# ─── Plugin discovery ─────────────────────────────────────────────────────────


class TestPluginDiscovery:
    def test_reads_requirements_txt(self, tmp_path: Path):
        plugin = tmp_path / "plugins" / "myplugin"
        plugin.mkdir(parents=True)
        (plugin / "requirements.txt").write_text("requests==2.20.0\n")
        components = sa._discover_plugins(tmp_path)
        assert len(components) == 1
        assert components[0].name == "requests"
        assert components[0].source == "plugin:myplugin"

    def test_skips_when_no_plugins_dir(self, tmp_path: Path):
        assert sa._discover_plugins(tmp_path) == []


# ─── OSV severity extraction ──────────────────────────────────────────────────


class TestSeverityExtraction:
    def test_database_specific_severity(self):
        rec = {"database_specific": {"severity": "HIGH"}}
        assert sa._osv_severity_from_record(rec) == "HIGH"


    def test_fixed_versions_extracted_and_deduped(self):
        rec = {
            "affected": [
                {
                    "ranges": [
                        {
                            "events": [
                                {"introduced": "0"},
                                {"fixed": "2.0.0"},
                            ]
                        }
                    ]
                },
                {"ranges": [{"events": [{"fixed": "2.0.0"}, {"fixed": "1.9.5"}]}]},
            ]
        }
        assert sa._osv_fixed_versions(rec) == ["2.0.0", "1.9.5"]


# ─── End-to-end orchestration with mocked OSV ─────────────────────────────────


class TestRunAudit:

    def test_findings_sorted_by_severity_desc(self, tmp_path: Path):
        plugin = tmp_path / "plugins" / "p"
        plugin.mkdir(parents=True)
        (plugin / "requirements.txt").write_text("alpha==1.0.0\nbeta==2.0.0\n")

        def fake_batch(comps):
            return {
                comps[0]: ["LOW-1"],
                comps[1]: ["CRIT-1"],
            }

        def fake_details(ids):
            return {
                "LOW-1": sa.Vulnerability(osv_id="LOW-1", severity="LOW", summary="low"),
                "CRIT-1": sa.Vulnerability(osv_id="CRIT-1", severity="CRITICAL", summary="crit"),
            }

        with patch.object(sa, "_osv_query_batch", side_effect=fake_batch), \
             patch.object(sa, "_osv_fetch_details", side_effect=fake_details):
            findings = sa.run_audit(
                skip_venv=True, skip_plugins=False, skip_mcp=True, hermes_home=tmp_path
            )
        assert len(findings) == 2
        # CRITICAL must come first
        assert findings[0].vuln.osv_id == "CRIT-1"
        assert findings[1].vuln.osv_id == "LOW-1"


# ─── CLI subcommand exit codes ────────────────────────────────────────────────


class TestExitCodes:
    def _build_args(self, **kwargs):
        import argparse

        defaults = {
            "skip_venv": True,
            "skip_plugins": True,
            "skip_mcp": True,
            "json": False,
            "fail_on": "critical",
        }
        defaults.update(kwargs)
        return argparse.Namespace(**defaults)





    def test_unknown_fail_on_value_exits_two(self, tmp_path: Path, monkeypatch, capsys):
        monkeypatch.setattr(sa, "get_hermes_home", lambda: str(tmp_path))
        code = sa.cmd_security_audit(self._build_args(fail_on="garbage"))
        assert code == 2
        err = capsys.readouterr().err
        assert "fail-on" in err.lower()

    def test_json_output_shape(self, tmp_path: Path, monkeypatch, capsys):
        monkeypatch.setattr(sa, "get_hermes_home", lambda: str(tmp_path))
        fake_comp = sa.Component(
            name="pkg", version="1.0", ecosystem="PyPI", source="venv"
        )
        monkeypatch.setattr(sa, "_discover_venv", lambda: [fake_comp])
        monkeypatch.setattr(
            sa, "_osv_query_batch", lambda comps: {fake_comp: ["X-1"]}
        )
        monkeypatch.setattr(
            sa,
            "_osv_fetch_details",
            lambda ids: {
                "X-1": sa.Vulnerability(
                    osv_id="X-1",
                    severity="HIGH",
                    summary="bad",
                    fixed_versions=["1.1"],
                )
            },
        )
        sa.cmd_security_audit(
            self._build_args(skip_venv=False, json=True, fail_on="critical")
        )
        payload = capsys.readouterr().out
        # The bitwarden banner can leak above the json; pick the first { line.
        lines = payload.splitlines()
        json_start = next(i for i, l in enumerate(lines) if l.startswith("{"))
        data = json.loads("\n".join(lines[json_start:]))
        assert data["finding_count"] == 1
        assert data["findings"][0]["severity"] == "HIGH"
        assert data["findings"][0]["fixed_versions"] == ["1.1"]


class TestMCPSecretAuditWiring:
    """Issue #91 rework: `hermes security audit` must run the MCP secret audit."""

    def _build_args(self, **kwargs):
        import argparse

        defaults = {
            "skip_venv": True,
            "skip_plugins": True,
            "skip_mcp": True,
            "json": False,
            "fail_on": "critical",
        }
        defaults.update(kwargs)
        return argparse.Namespace(**defaults)

    def test_mcp_secret_findings_surface_in_human_output(
        self, tmp_path: Path, monkeypatch, capsys
    ):
        """Plaintext-secret findings from the MCP audit appear in the report."""
        monkeypatch.setattr(sa, "get_hermes_home", lambda: str(tmp_path))
        monkeypatch.setattr(sa, "_discover_venv", lambda: [])
        monkeypatch.setattr(sa, "_osv_query_batch", lambda comps: {})
        fake = [{
            "server": "graphiti",
            "field": "env.NEO4J_PASSWORD",
            "kind": "plaintext-secret",
            "hint": "<15 chars>",
        }]
        monkeypatch.setattr(sa, "_run_mcp_secret_audit", lambda home: (fake, 1))

        code = sa.cmd_security_audit(self._build_args(skip_venv=False))
        out = capsys.readouterr().out
        assert "MCP secret audit: 1 server(s) scanned, 1 finding(s)" in out
        assert "[plaintext-secret] graphiti.env.NEO4J_PASSWORD -> <15 chars>" in out
        assert code == 1  # any plaintext credential is actionable

    def test_mcp_secret_findings_surface_in_json(
        self, tmp_path: Path, monkeypatch, capsys
    ):
        monkeypatch.setattr(sa, "get_hermes_home", lambda: str(tmp_path))
        monkeypatch.setattr(sa, "_discover_venv", lambda: [])
        monkeypatch.setattr(sa, "_osv_query_batch", lambda comps: {})
        fake = [{
            "server": "remote",
            "field": "headers.Authorization",
            "kind": "plaintext-secret",
            "hint": "<18 chars>",
        }]
        monkeypatch.setattr(sa, "_run_mcp_secret_audit", lambda home: (fake, 1))

        sa.cmd_security_audit(
            self._build_args(skip_venv=False, json=True, fail_on="critical")
        )
        payload = capsys.readouterr().out
        lines = payload.splitlines()
        json_start = next(i for i, l in enumerate(lines) if l.startswith("{"))
        data = json.loads("\n".join(lines[json_start:]))
        assert data["mcp_secret_servers_scanned"] == 1
        assert data["mcp_secret_findings"] == fake

    def test_clean_mcp_audit_keeps_exit_zero(self, tmp_path: Path, monkeypatch, capsys):
        monkeypatch.setattr(sa, "get_hermes_home", lambda: str(tmp_path))
        monkeypatch.setattr(sa, "_discover_venv", lambda: [])
        monkeypatch.setattr(sa, "_osv_query_batch", lambda comps: {})
        monkeypatch.setattr(sa, "_run_mcp_secret_audit", lambda home: ([], 2))

        code = sa.cmd_security_audit(self._build_args(skip_venv=False))
        out = capsys.readouterr().out
        assert "MCP secret audit: 2 server(s) scanned, no findings" in out
        assert code == 0

    def test_missing_script_degrades_gracefully(self, tmp_path: Path, monkeypatch, capsys):
        """A missing script must not fail the OSV audit."""
        monkeypatch.setattr(sa, "get_hermes_home", lambda: str(tmp_path))
        monkeypatch.setattr(sa, "_discover_venv", lambda: [])
        monkeypatch.setattr(sa, "_osv_query_batch", lambda comps: {})
        monkeypatch.setattr(sa, "_load_mcp_secret_audit_module", lambda: None)

        code = sa.cmd_security_audit(self._build_args(skip_venv=False))
        capsys.readouterr()
        assert code == 0

    def test_end_to_end_against_real_script(self, tmp_path: Path):
        """The real script, loaded through the CLI path, flags a plaintext secret."""
        cfg = tmp_path / "config.yaml"
        cfg.write_text(
            "mcp_servers:\n"
            "  graphiti:\n"
            "    env:\n"
            "      NEO4J_PASSWORD: graphiti-pass\n",
            encoding="utf-8",
        )
        findings, scanned = sa._run_mcp_secret_audit(tmp_path)
        assert scanned == 1
        assert any(
            f["kind"] == "plaintext-secret" and f["server"] == "graphiti"
            for f in findings
        )
        # The hint must never leak the value itself.
        assert all("graphiti-pass" not in f["hint"] for f in findings)
