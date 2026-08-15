"""Unit tests for typed skill contract extraction (#2414 / parent #2382).

Verifies contract extraction across sample built-in skills and synthetic test skills.
"""

from pathlib import Path

from agent.skill_contract import (
    SkillContract,
    SkillInterface,
    SkillScopedRule,
    SkillToolProtocol,
    SkillWorkflowStep,
    extract_skill_contract,
)


class TestSkillContractExtraction:
    """Test structural contract extraction across real and synthetic skills."""

    def test_extract_synthetic_skill_contract(self):
        sample = """---
name: sample-deployer
description: "Deploys services to cloud environments."
version: 2.1.0
platforms: [linux, macos]
metadata:
  hermes:
    tags: [Deploy, Cloud, DevOps]
---

# Deployer Workflow

## Prerequisites
- Valid API token set in GITHUB_TOKEN
- Docker daemon running locally

## 1. Build Container
Build the docker container:

```bash
export DOCKER_TAG="v2.1"
docker build -t app:$DOCKER_TAG .
```

## 2. Run Tests
Verify container:

```bash
pytest tests/
```
"""
        contract = extract_skill_contract(sample)
        assert isinstance(contract, SkillContract)
        assert contract.name == "sample-deployer"
        assert contract.interface.version == "2.1.0"
        assert contract.interface.platforms == ["linux", "macos"]
        assert "Deploy" in contract.interface.tags

        # Rules
        assert len(contract.rules) == 2
        assert contract.rules[0].category == "prerequisite"
        assert "GITHUB_TOKEN" in contract.rules[0].rule

        # Workflow steps
        assert len(contract.workflow) >= 2
        titles = [step.title for step in contract.workflow]
        assert any("Build Container" in t for t in titles)
        assert any("Run Tests" in t for t in titles)

        # Tools & Environment variables
        assert "docker" in contract.tools.required_tools
        assert "pytest" in contract.tools.required_tools
        assert "DOCKER_TAG" in contract.tools.environment_variables

        # Dict / JSON serialization
        d = contract.to_dict()
        assert d["name"] == "sample-deployer"
        j = contract.to_json()
        assert '"sample-deployer"' in j

    def test_extract_github_pr_workflow_skill(self):
        path = Path("skills/github/github-pr-workflow/SKILL.md")
        if not path.exists():
            return
        content = path.read_text(encoding="utf-8")
        contract = extract_skill_contract(content)

        assert contract.name == "github-pr-workflow"
        assert contract.interface.description != ""
        assert len(contract.workflow) > 0
        assert len(contract.rules) > 0
        assert (
            "gh" in contract.tools.required_tools
            or "git" in contract.tools.required_tools
        )
        assert contract.raw_char_count == len(content.strip())

    def test_extract_github_auth_skill(self):
        path = Path("skills/github/github-auth/SKILL.md")
        if not path.exists():
            return
        content = path.read_text(encoding="utf-8")
        contract = extract_skill_contract(content)

        assert contract.name == "github-auth"
        assert (
            "Authentication" in contract.interface.tags
            or "GitHub" in contract.interface.tags
        )
        assert len(contract.workflow) > 0
        assert (
            "git" in contract.tools.required_tools
            or "gh" in contract.tools.required_tools
        )

    def test_compress_skill_mdl_preserves_critical_rules(self):
        from agent.skill_contract import compress_skill_mdl, validate_skill_contract

        verbose_skill = """---
name: security-auditor
description: "Perform comprehensive security audit on code repository."
version: 1.0.0
platforms: [linux, macos]
metadata:
  hermes:
    tags: [Security, Audit]
---

# Security Auditor

This is an extensive introductory paragraph explaining the philosophical importance of security audits in large distributed multi-tenant cloud agent systems. It discusses history, general principles, and best practices at great length without adding specific actionable rules.

## Prerequisites & Constraints
- [CRITICAL] Never log unmasked authorization tokens to disk
- [CONSTRAINT] Always run audit inside an isolated container sandbox
- [RULE] Report CVE severity scores using CVSS v3.1 standard

## 1. Scan Dependencies
First we explain why scanning dependencies is very critical in modern supply chain security ecosystems. We give multiple historical examples.

```bash
safety check --full-report
```

## 2. Scan Code
Here is another large discursive section discussing AST analysis and regex pattern searching.

```bash
bandit -r src/
```
"""
        compressed = compress_skill_mdl(verbose_skill)
        assert len(compressed) < len(verbose_skill)
        # Verify rare-but-critical rules survived compression
        assert "Never log unmasked authorization tokens to disk" in compressed
        assert "isolated container sandbox" in compressed
        assert "CVSS v3.1" in compressed
        assert "safety check --full-report" in compressed
        assert "bandit -r src/" in compressed

        # Verify contract validation passes
        orig_contract = extract_skill_contract(verbose_skill)
        is_valid, violations = validate_skill_contract(orig_contract, compressed)
        assert is_valid is True
        assert len(violations) == 0

    def test_validate_skill_contract_detects_violations(self):
        from agent.skill_contract import validate_skill_contract

        orig = """---
name: test-skill
description: "Test skill."
---

## Rules
- Critical security barrier: do not bypass auth proxy

## 1. Step
```bash
curl -s http://api.internal
```
"""
        bad_compressed = """---
name: wrong-name
description: "Test skill."
---

## 1. Step
```bash
curl -s http://api.internal
```
"""
        contract = extract_skill_contract(orig)
        is_valid, violations = validate_skill_contract(contract, bad_compressed)
        assert is_valid is False
        assert any("Name mismatch" in v for v in violations)
        assert any("Missing critical rule" in v for v in violations)
