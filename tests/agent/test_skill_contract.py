# -*- coding: utf-8 -*-
"""Tests for agent/skill_contract.py — Slice A of SkillZip (#2414).

Covers the issue's acceptance criteria:
- extractor returns a typed contract object from skill markdown
- contract covers interface, workflow, tool protocol, scoped rules
- unit tests over 3 sample skills (minimal, full-featured, malformed)
"""

from __future__ import annotations

import json

import pytest

from agent.skill_contract import SkillContract, extract_contract

# ---------------------------------------------------------------------------
# Sample skills
# ---------------------------------------------------------------------------

MINIMAL_SKILL = """---
name: minimal-skill
description: "Use when doing a minimal thing."
version: 1.0.0
author: Hermes Agent
license: MIT
metadata:
  hermes:
    tags: [minimal]
---
# Minimal Skill
## Overview
One paragraph of what and why.

## When to Use
- User asks for a minimal thing
- Don't use for: anything complex
"""

FULL_SKILL = """---
name: deploy-service
description: "Use when deploying a service to production."
version: 2.1.0
author: Hermes Agent
license: Apache-2.0
metadata:
  hermes:
    tags: [deploy, ops]
    related_skills: [plan]
---
# Deploy Service
## Overview
Deploys a service end to end.

## When to Use
- User asks to deploy a service
- A release is approved
- Don't use for: local dev setups

## Workflow
1. Run the preflight checks with `terminal`
2. Review the release notes with `read_file`
3. Tag the release via `git`
4. Verify with the monitoring dashboard

## Common Pitfalls
1. Skipping preflight checks — deploy fails at the gate
2. Forgetting the `--no-cache` flag — stale image ships

## Verification Checklist
- [ ] Preflight passed
- [ ] Dashboard shows the new version
"""

MALFORMED_SKILL = """---
name: [broken
description: - item
  nested: [unclosed
version: 1..2
license:
---
## Workflow
1. only step

not a list, no heading above this
"""

# ---------------------------------------------------------------------------
# Acceptance criterion 1: typed contract object from skill markdown
# ---------------------------------------------------------------------------


class TestTypedContract:
    def test_returns_skill_contract_instance(self):
        assert isinstance(extract_contract(MINIMAL_SKILL), SkillContract)

    def test_interface_fields_from_frontmatter(self):
        c = extract_contract(FULL_SKILL)
        assert c.skill_name == "deploy-service"
        assert "deploying a service" in c.description
        assert c.version == "2.1.0"
        assert c.license == "Apache-2.0"

    def test_interface_fields_empty_when_frontmatter_missing(self):
        c = extract_contract("# Just a title\n## Overview\nbody\n")
        assert c.skill_name == ""
        assert c.description == ""

    def test_roundtrip_via_json(self):
        c = extract_contract(FULL_SKILL)
        restored = SkillContract.from_dict(json.loads(json.dumps(c.to_dict())))
        assert restored == c

    def test_from_dict_tolerates_malformed_fields(self):
        restored = SkillContract.from_dict({
            "skill_name": None,
            "workflow": "not-a-list",
            "source_chars": "x",
        })
        assert restored.skill_name == ""
        assert restored.workflow == []
        assert restored.source_chars == 0


# ---------------------------------------------------------------------------
# Acceptance criterion 2: contract covers the four contract areas
# ---------------------------------------------------------------------------


class TestContractCoverage:
    def test_workflow_steps_extracted(self):
        c = extract_contract(FULL_SKILL)
        assert any("preflight" in step.lower() for step in c.workflow)
        assert len(c.workflow) == 4

    def test_workflow_empty_when_no_workflow_section(self):
        assert extract_contract(MINIMAL_SKILL).workflow == []

    def test_tools_extracted_and_deduped(self):
        c = extract_contract(FULL_SKILL)
        assert "terminal" in c.tools
        assert "read_file" in c.tools
        assert len(c.tools) == len(set(c.tools))

    def test_scoped_rules_from_pitfalls_checklist_when_to_use(self):
        c = extract_contract(FULL_SKILL)
        rules_text = " ".join(c.scoped_rules).lower()
        assert "skipping preflight" in rules_text
        assert "release is approved" in rules_text
        assert "preflight passed" in rules_text


# ---------------------------------------------------------------------------
# Acceptance criterion 3: three sample skills, including malformed input
# ---------------------------------------------------------------------------


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
        assert c.description.startswith("Use when")
        assert c.section_titles == ["Overview", "When to Use"]
        assert "User asks for a minimal thing" in c.scoped_rules

    def test_full_skill_contract(self):
        c = extract_contract(FULL_SKILL)
        assert c.skill_name == "deploy-service"
        assert len(c.workflow) == 4
        assert set(c.tools) >= {"terminal", "read_file", "git"}
        assert c.source_chars == len(FULL_SKILL)

    def test_malformed_skill_never_raises_and_degrades(self):
        c = extract_contract(MALFORMED_SKILL)
        assert c.skill_name == ""  # unparseable frontmatter → empty interface
        assert len(c.workflow) == 1
        assert c.source_chars == len(MALFORMED_SKILL)

    def test_empty_input(self):
        assert extract_contract("") == SkillContract()
