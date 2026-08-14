"""Tests for agent/skill_contract.py — Slice A of SkillZip (#2414).

Acceptance criteria: typed contract object; covers interface, workflow,
tool protocol, scoped rules; tests over 3 sample skills.
"""

from __future__ import annotations

import pytest

from agent.skill_contract import SkillContract, extract_contract

MINIMAL_SKILL = """---
name: minimal-skill
description: "Use when doing a minimal thing."
version: 1.0.0
---
## When to Use
- User asks for a minimal thing
"""

FULL_SKILL = """---
name: deploy-service
description: "Use when deploying a service to production."
version: 2.1.0
---
## When to Use
- A release is approved

## Workflow
1. Run the preflight checks with `terminal`
2. Review the release notes with `read_file`
3. Tag the release via `git`

## Common Pitfalls
- Skipping preflight checks — deploy fails at the gate
"""

MALFORMED_SKILL = """---[broken
name: [unclosed
---
## Workflow
1. only step
"""


class TestTypedContract:
    def test_interface_fields_from_frontmatter(self):
        c = extract_contract(FULL_SKILL)
        assert isinstance(c, SkillContract)
        assert c.skill_name == "deploy-service"
        assert "deploying a service" in c.description
        assert c.version == "2.1.0"


class TestContractCoverage:
    def test_workflow_steps_extracted(self):
        c = extract_contract(FULL_SKILL)
        assert len(c.workflow) == 3
        assert any("preflight" in s.lower() for s in c.workflow)

    def test_tools_extracted_and_deduped(self):
        c = extract_contract(FULL_SKILL)
        assert {"terminal", "read_file", "git"} <= set(c.tools)
        assert len(c.tools) == len(set(c.tools))

    def test_scoped_rules_from_when_to_use_and_pitfalls(self):
        c = extract_contract(FULL_SKILL)
        rules = " ".join(c.scoped_rules).lower()
        assert "skipping preflight" in rules
        assert "release is approved" in rules


class TestThreeSampleSkills:
    @pytest.mark.parametrize(
        "sample",
        [MINIMAL_SKILL, FULL_SKILL, MALFORMED_SKILL],
        ids=["minimal", "full", "malformed"],
    )
    def test_never_raises_on_any_sample(self, sample):
        extract_contract(sample)

    def test_minimal_skill_contract(self):
        c = extract_contract(MINIMAL_SKILL)
        assert c.skill_name == "minimal-skill"
        assert c.workflow == []  # no Workflow section → empty workflow
        assert "User asks for a minimal thing" in c.scoped_rules

    def test_malformed_skill_degrades(self):
        c = extract_contract(MALFORMED_SKILL)
        assert c.skill_name == ""  # unparseable frontmatter → empty interface
        assert len(c.workflow) == 1
