"""Tests for scripts/evolution_pre_submit_triage.py — shift-left dedup gate (#336).

The gate decides CREATE / SKIP-duplicate for a DRAFT proposal BEFORE
`gh issue create`, comparing it against the currently-OPEN issues. The
CRITICAL invariant under test is CONSERVATISM: skip ONLY on a high-confidence
title overlap; default to CREATE on any doubt. This is the anti-fabrication
guard — the project's documented past failure mode (#83/#101) was triage
FABRICATING a rejection and wrongly closing a real issue. A weak/ambiguous
match must therefore yield CREATE, never SKIP.

Pure + offline: the open-issues list is INJECTED (no network), so these tests
run with no `gh`.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from evolution_pre_submit_triage import (  # noqa: E402
    DEFAULT_THRESHOLD,
    GateDecision,
    decide,
    similarity,
)


def _issue(number, title, state="open"):
    return {"number": number, "title": title, "state": state}


class TestSimilarity:
    def test_identical_titles_score_one(self):
        # Cosmetic-only variants (tag, case, punctuation) normalize to the SAME
        # canonical token set -> 1.0. Note: stripping "[FIX]" is NOT the same as
        # the word "fix" surviving, so the tag form and a literal "fix ..." form
        # are deliberately < 1.0 (see test_tag_strip_is_not_word_fix).
        assert similarity("[FIX] Web Scraping 403!", "[fix]  web scraping 403.") == 1.0

    def test_tag_strip_is_not_word_fix(self):
        # Guard the normalization contract: a stripped [TAG] != the bare word.
        s = similarity("[FIX] web scraping 403", "fix web scraping 403")
        assert 0.0 < s < 1.0

    def test_disjoint_titles_score_zero(self):
        assert similarity("add memory cache", "rewrite the cron scheduler") == 0.0

    def test_partial_overlap_between_zero_and_one(self):
        s = similarity("add LRU memory cache", "add memory cache eviction")
        assert 0.0 < s < 1.0

    def test_symmetric(self):
        a = similarity("alpha beta gamma", "beta gamma delta")
        b = similarity("beta gamma delta", "alpha beta gamma")
        assert a == b

    def test_empty_titles_are_safe_zero(self):
        # No tokens -> no evidence of duplication -> 0.0 (never a skip signal).
        assert similarity("", "anything") == 0.0
        assert similarity("[TAG]", "") == 0.0


class TestDecideSkipOnHighConfidence:
    def test_exact_duplicate_skips(self):
        draft = {"title": "[IMPROVEMENT] Per-cycle funnel metrics"}
        open_issues = [_issue(42, "per-cycle funnel metrics")]
        d = decide(draft, open_issues)
        assert isinstance(d, GateDecision)
        assert d.decision == "skip_duplicate"
        assert d.matched_issue == 42
        assert d.score == 1.0

    def test_near_duplicate_above_threshold_skips(self):
        # Cosmetic word added but the signature is overwhelmingly shared.
        draft = {"title": "[FEATURE] add hierarchical memory cache with LRU eviction"}
        open_issues = [_issue(7, "add hierarchical memory cache with LRU eviction now")]
        d = decide(draft, open_issues)
        assert d.decision == "skip_duplicate"
        assert d.matched_issue == 7
        assert d.score >= DEFAULT_THRESHOLD

    def test_skip_picks_best_of_several(self):
        draft = {"title": "Add per-cycle funnel metrics dashboard"}
        open_issues = [
            _issue(1, "rewrite the cron scheduler"),
            _issue(2, "add per-cycle funnel metrics dashboard"),  # exact
            _issue(3, "add some metrics"),
        ]
        d = decide(draft, open_issues)
        assert d.decision == "skip_duplicate"
        assert d.matched_issue == 2


class TestDecideCreateOnDoubt:
    """The anti-fabrication guard: anything short of a HIGH-confidence overlap
    must default to CREATE. Never skip on a weak match (#83/#101 lesson)."""

    def test_weak_match_creates_not_skips(self):
        # Shares only the generic word "metrics" — a weak, ambiguous overlap.
        draft = {"title": "[FEATURE] Realtime GPU utilization metrics"}
        open_issues = [_issue(9, "Add per-cycle funnel metrics")]
        d = decide(draft, open_issues)
        assert d.decision == "create"
        assert d.matched_issue is None
        assert d.score < DEFAULT_THRESHOLD

    def test_ambiguous_partial_overlap_creates(self):
        # Real, related, but clearly a DIFFERENT proposal — must not be skipped.
        draft = {"title": "Add retry backoff to provider error handling"}
        open_issues = [_issue(11, "Add circuit breaker to provider error handling")]
        d = decide(draft, open_issues)
        assert d.decision == "create"

    def test_empty_open_issues_creates(self):
        d = decide({"title": "anything at all"}, [])
        assert d.decision == "create"
        assert d.matched_issue is None
        assert d.score == 0.0

    def test_only_open_issues_considered_for_skip(self):
        # A CLOSED issue with an exact title must NOT trigger a skip here:
        # the OPEN-issue dedup scope is the only thing this gate owns; closed/
        # rejected handling stays in the SKILL.md (and re-filing a closed-as-
        # rejected idea is a separate, already-covered concern). Default CREATE.
        draft = {"title": "Exactly the same title"}
        open_issues = [_issue(5, "exactly the same title", state="closed")]
        d = decide(draft, open_issues)
        assert d.decision == "create"
        assert d.matched_issue is None

    def test_just_below_threshold_creates(self):
        # Construct a match strictly below threshold -> CREATE (boundary is safe).
        draft = {"title": "alpha beta gamma delta"}
        open_issues = [_issue(3, "alpha beta gamma epsilon zeta")]
        s = similarity(draft["title"], open_issues[0]["title"])
        assert s < DEFAULT_THRESHOLD, "fixture must sit below threshold"
        d = decide(draft, open_issues)
        assert d.decision == "create"


class TestDecideThresholdConfigurable:
    def test_threshold_is_injectable(self):
        draft = {"title": "alpha beta gamma delta"}
        open_issues = [_issue(3, "alpha beta gamma epsilon zeta")]
        s = similarity(draft["title"], open_issues[0]["title"])
        # With a permissive threshold at/below the score, the SAME pair skips.
        d = decide(draft, open_issues, threshold=s)
        assert d.decision == "skip_duplicate"
        assert d.matched_issue == 3

    def test_default_threshold_is_high(self):
        # Conservatism is a design constant, not an accident: keep the bar high.
        assert DEFAULT_THRESHOLD >= 0.8


class TestGateDecisionShape:
    def test_decision_is_serializable_object(self):
        d = decide({"title": "x y z"}, [])
        # Structured gate object {decision, matched_issue, score, reason}.
        as_dict = d.to_dict()
        assert set(as_dict) == {"decision", "matched_issue", "score", "reason"}
        assert as_dict["decision"] == "create"
        assert isinstance(as_dict["reason"], str) and as_dict["reason"]

    def test_draft_title_missing_is_create(self):
        # A draft with no usable title can't be proven a duplicate -> CREATE.
        d = decide({}, [_issue(1, "some open issue")])
        assert d.decision == "create"
