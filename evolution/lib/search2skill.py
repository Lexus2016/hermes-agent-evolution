# -*- coding: utf-8 -*-
"""External-source skill capture via Search2Skill distillation (issue #2291).

Implements Search2Skill architecture:
1. Detects agent capability gaps beyond parametric boundaries from execution errors.
2. Generates targeted external queries (docs, API specifications, community patterns).
3. Distills retrieved external evidence into structured, validated SKILL.md specs.
"""

from __future__ import annotations

import logging
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

__all__ = [
    "CapabilityGap",
    "ExternalSkillSpec",
    "Search2SkillEngine",
]


@dataclass
class CapabilityGap:
    """A detected capability gap in agent toolsets or operational knowledge."""

    domain: str
    gap_description: str
    unsupported_operations: List[str] = field(default_factory=list)
    confidence_score: float = 0.8

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class ExternalSkillSpec:
    """A distilled skill specification synthesized from external documentation."""

    skill_name: str
    target_domain: str
    external_source_url: str
    distilled_markdown: str
    gap_addressed: CapabilityGap

    def to_dict(self) -> Dict[str, Any]:
        return {
            "skill_name": self.skill_name,
            "target_domain": self.target_domain,
            "external_source_url": self.external_source_url,
            "distilled_markdown": self.distilled_markdown,
            "gap_addressed": self.gap_addressed.to_dict(),
        }


class Search2SkillEngine:
    """Capability-gap detector and external-evidence skill synthesizer."""

    COMMON_DOMAINS = {
        "docker": ["docker", "container", "dockerfile", "compose"],
        "git": ["git", "rebase", "submodule", "cherry-pick"],
        "database": ["sql", "postgres", "sqlite", "migration", "alembic"],
        "kubernetes": ["k8s", "kubectl", "helm", "pod", "deployment"],
        "aws": ["aws", "s3", "lambda", "boto3", "iam"],
    }

    @classmethod
    def detect_capability_gaps(
        cls,
        error_logs: Sequence[str],
        available_skills: Sequence[str] = (),
    ) -> List[CapabilityGap]:
        """Analyze failure logs to extract missing external domain capabilities."""
        gaps: List[CapabilityGap] = []
        available_set = {s.lower() for s in available_skills}

        for log_entry in error_logs:
            entry_lower = log_entry.lower()
            for domain, keywords in cls.COMMON_DOMAINS.items():
                if domain in available_set:
                    continue

                matched_keywords = [kw for kw in keywords if kw in entry_lower]
                if matched_keywords:
                    # Found gap in domain
                    existing = next((g for g in gaps if g.domain == domain), None)
                    if existing:
                        for kw in matched_keywords:
                            if kw not in existing.unsupported_operations:
                                existing.unsupported_operations.append(kw)
                    else:
                        gaps.append(
                            CapabilityGap(
                                domain=domain,
                                gap_description=f"Missing operational knowledge for {domain} operations",
                                unsupported_operations=list(matched_keywords),
                                confidence_score=min(
                                    1.0, 0.6 + len(matched_keywords) * 0.1
                                ),
                            )
                        )
        return gaps

    @classmethod
    def generate_search_queries(cls, gap: CapabilityGap) -> List[str]:
        """Construct high-relevance search queries for external knowledge retrieval."""
        queries: List[str] = []
        queries.append(f"{gap.domain} CLI best practices documentation cheat sheet")
        for op in gap.unsupported_operations:
            queries.append(f"how to {op} command line reference guide")
        return queries

    @classmethod
    def distill_external_evidence(
        cls,
        gap: CapabilityGap,
        raw_evidence: str,
        source_url: str = "https://docs.example.com",
    ) -> ExternalSkillSpec:
        """Distill unstructured external documentation into canonical SKILL.md."""
        clean_domain = re.sub(r"[^a-zA-Z0-9_-]", "-", gap.domain.lower()).strip("-")
        skill_name = f"{clean_domain}-operations"
        description = f"External {gap.domain.capitalize()} operational guidelines."[:60]

        frontmatter = f"""---
name: {skill_name}
description: {description}
version: 1.0.0
author: Search2Skill Engine
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [{clean_domain}, external-distilled]
    category: operations
---
"""
        body = f"""# {clean_domain.replace("-", " ").title()} Operations

## Overview
{description} Distilled from external evidence: {source_url}.

## Supported Operations
- {", ".join(gap.unsupported_operations) if gap.unsupported_operations else "General operations"}

## Reference Guide
{raw_evidence.strip()[:1500]}

## Verification
- Verify successful execution with returncode 0.
"""
        full_md = frontmatter.strip() + "\n\n" + body.strip() + "\n"

        return ExternalSkillSpec(
            skill_name=skill_name,
            target_domain=gap.domain,
            external_source_url=source_url,
            distilled_markdown=full_md,
            gap_addressed=gap,
        )
