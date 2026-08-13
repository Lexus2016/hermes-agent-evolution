# -*- coding: utf-8 -*-
"""Tests for the leave-one-out utility audit (issue #2286, SkillProx)."""

import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from evolution_utility_audit import (  # noqa: E402
    KEEP_FRACTION,
    audit_corpus,
    skill_utility,
)


def _now():
    return datetime.now(timezone.utc)


def _rec(use=0, view=0, patch=0, last=None, description=""):
    d: dict = {"use_count": use, "view_count": view, "patch_count": patch}
    if last is not None:
        d["last_used_at"] = last.isoformat()
    if description:
        d["description"] = description
    return d


class TestSkillUtility:
    def test_zero_activity_scores_zero(self):
        assert skill_utility(_rec()) == 0.0

    def test_recent_activity_scores_full(self):
        now = _now()
        assert skill_utility(_rec(use=10, last=now), now) == 10.0

    def test_high_utility_skill_is_kept(self):
        now = _now()
        usage = {"workhorse": _rec(use=100, last=now), "minor": _rec(use=1, last=now)}
        by_name = {a.name: a for a in audit_corpus(usage, now)}
        assert by_name["workhorse"].verdict == "keep"
        assert by_name["workhorse"].share >= KEEP_FRACTION

    def test_inert_skill_is_removed(self):
        now = _now()
        usage = {"active": _rec(use=50, last=now), "inert": _rec()}
        by_name = {a.name: a for a in audit_corpus(usage, now)}
        assert by_name["inert"].verdict == "remove"

    def test_redundant_low_utility_is_consolidated(self):
        now = _now()
        usage = {
            "main": _rec(use=100, last=now),
            "parse-json": _rec(
                use=1, last=now, description="parse json data structures"
            ),
            "json-parse": _rec(
                use=1, last=now, description="parse json data structures"
            ),
        }
        by_name = {a.name: a for a in audit_corpus(usage, now)}
        assert by_name["parse-json"].max_overlap > 0.35
        assert by_name["parse-json"].verdict == "consolidate"

    def test_low_utility_non_redundant_is_demoted(self):
        now = _now()
        usage = {
            "main": _rec(use=100, last=now),
            "niche": _rec(
                use=1, last=now, description="completely unrelated domain topic"
            ),
        }
        by_name = {a.name: a for a in audit_corpus(usage, now)}
        assert by_name["niche"].verdict == "demote"
