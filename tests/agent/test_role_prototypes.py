# -*- coding: utf-8 -*-
"""Unit tests for ExRole role prototypes induction and suggestion (#2383)."""

from pathlib import Path
from agent.role_prototypes import (
    RolePrototype,
    induce_role_prototypes_from_trajectories,
    load_role_prototypes,
    save_role_prototypes,
    suggest_delegation_role,
)


class TestRolePrototypes:
    """Test suite for RolePrototype induction, serialization, and suggestion."""

    def test_serialization_roundtrip(self):
        proto = RolePrototype(
            name="SecurityScanner",
            description="Scans repository for vulnerabilities",
            target_tasks=["run security audit", "cve check"],
            suggested_tools=["read_file", "terminal"],
            context_keys=["repo_path", "cve_list"],
            recommended_model="anthropic/claude-3-5-sonnet",
            success_rate=0.92,
            evidence_count=12,
        )
        d = proto.to_dict()
        assert d["name"] == "SecurityScanner"
        assert d["success_rate"] == 0.92

        restored = RolePrototype.from_dict(d)
        assert restored.name == proto.name
        assert restored.suggested_tools == proto.suggested_tools
        assert restored.evidence_count == 12

    def test_induce_from_trajectories(self):
        trajectories = [
            {
                "role": "code_auditor",
                "task_type": "security audit",
                "tools_used": ["read_file", "search_files"],
                "context_keys": ["workspace_root"],
                "model": "claude-3-7-sonnet",
                "success": True,
            },
            {
                "role": "code_auditor",
                "task_type": "vulnerability scan",
                "tools_used": ["read_file", "terminal"],
                "context_keys": ["workspace_root"],
                "model": "claude-3-7-sonnet",
                "success": True,
            },
            {
                "role": "code_auditor",
                "task_type": "ast review",
                "tools_used": ["read_file"],
                "context_keys": ["workspace_root"],
                "model": "claude-3-7-sonnet",
                "success": False,
            },
            {
                "role": "test_runner",
                "task_type": "run pytest",
                "tools_used": ["terminal"],
                "model": "gpt-4o",
                "success": True,
            },
        ]

        prototypes = induce_role_prototypes_from_trajectories(trajectories)
        assert len(prototypes) >= 2

        auditor = next(p for p in prototypes if p.name == "CodeAuditor")
        assert auditor.evidence_count == 3
        assert pytest_approx(auditor.success_rate, 2 / 3)
        assert "read_file" in auditor.suggested_tools
        assert "workspace_root" in auditor.context_keys
        assert auditor.recommended_model == "claude-3-7-sonnet"

        runner = next(p for p in prototypes if p.name == "TestRunner")
        assert runner.evidence_count == 1
        assert runner.success_rate == 1.0
        assert "terminal" in runner.suggested_tools

    def test_suggest_delegation_role(self):
        prototypes = [
            RolePrototype(
                name="SecurityScanner",
                description="Scans security issues",
                target_tasks=["security audit", "vulnerability scan"],
                suggested_tools=["read_file"],
                success_rate=0.9,
            ),
            RolePrototype(
                name="WebResearcher",
                description="Performs web research and documentation synthesis",
                target_tasks=["search docs", "fetch web citations"],
                suggested_tools=["web_search"],
                success_rate=0.85,
            ),
        ]

        # Security query should suggest SecurityScanner
        suggestion = suggest_delegation_role(
            "Perform vulnerability scan and security audit on dependencies",
            prototypes=prototypes,
        )
        assert suggestion is not None
        assert suggestion.name == "SecurityScanner"

        # Web query should suggest WebResearcher
        suggestion2 = suggest_delegation_role(
            "Search docs for API reference and citations",
            prototypes=prototypes,
        )
        assert suggestion2 is not None
        assert suggestion2.name == "WebResearcher"

        # Irrelevant query should return None
        suggestion3 = suggest_delegation_role(
            "xyz completely unrelated abc",
            prototypes=prototypes,
        )
        assert suggestion3 is None


def pytest_approx(val, target, tolerance=0.01):
    return abs(val - target) <= tolerance
