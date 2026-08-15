# -*- coding: utf-8 -*-
"""Unit tests for Search2Skill External-Source Skill Capture (#2291)."""

import pytest

from evolution.lib.search2skill import (
    CapabilityGap,
    ExternalSkillSpec,
    Search2SkillEngine,
)


class TestSearch2SkillEngine:
    """Test suite for Search2Skill detection and distillation."""

    def test_detect_capability_gaps(self):
        error_logs = [
            "Error: failed to apply alembic database migration",
            "Command failed: kubectl apply -f pod.yaml",
        ]
        gaps = Search2SkillEngine.detect_capability_gaps(
            error_logs, available_skills=["docker"]
        )
        domains = [g.domain for g in gaps]
        assert "database" in domains
        assert "kubernetes" in domains
        assert "docker" not in domains

    def test_generate_search_queries(self):
        gap = CapabilityGap(
            domain="kubernetes",
            gap_description="Missing kubectl deployment skills",
            unsupported_operations=["kubectl", "helm"],
        )
        queries = Search2SkillEngine.generate_search_queries(gap)
        assert len(queries) >= 2
        assert any("kubernetes" in q for q in queries)
        assert any("helm" in q for q in queries)

    def test_distill_external_evidence(self):
        gap = CapabilityGap(
            domain="kubernetes",
            gap_description="Missing kubectl deployment skills",
            unsupported_operations=["kubectl", "helm"],
        )
        raw_doc = "To apply a manifest: kubectl apply -f <file>. To deploy a chart: helm install <release> <chart>."
        spec = Search2SkillEngine.distill_external_evidence(
            gap=gap,
            raw_evidence=raw_doc,
            source_url="https://kubernetes.io/docs",
        )
        assert spec.skill_name == "kubernetes-operations"
        assert "Search2Skill Engine" in spec.distilled_markdown
        assert "kubectl apply -f" in spec.distilled_markdown

        # Check frontmatter rules (description <= 60 chars)
        lines = spec.distilled_markdown.splitlines()
        desc_line = [l for l in lines if l.startswith("description:")][0]
        desc_val = desc_line.replace("description:", "").strip()
        assert len(desc_val) <= 60
