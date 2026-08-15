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
