"""Tests for tools/skill_provenance.py — write-origin ContextVar."""

import contextvars


def test_set_and_get_origin():
    from tools.skill_provenance import (
        set_current_write_origin,
        reset_current_write_origin,
        get_current_write_origin,
    )
    token = set_current_write_origin("background_review")
    try:
        assert get_current_write_origin() == "background_review"
    finally:
        reset_current_write_origin(token)


def test_empty_origin_falls_back_to_foreground():
    from tools.skill_provenance import (
        set_current_write_origin,
        reset_current_write_origin,
        get_current_write_origin,
    )
    token = set_current_write_origin("")
    try:
        # Empty is coerced to "foreground" at the set() boundary.
        assert get_current_write_origin() == "foreground"
    finally:
        reset_current_write_origin(token)


def test_context_isolation_between_copies():
    """ContextVar scoping: modifications in one copy do not leak out."""
    from tools.skill_provenance import (
        set_current_write_origin,
        get_current_write_origin,
        BACKGROUND_REVIEW,
    )

    # Start at the module default.
    original = get_current_write_origin()

    def _run_in_copy():
        set_current_write_origin(BACKGROUND_REVIEW)
        return get_current_write_origin()

    ctx = contextvars.copy_context()
    inside = ctx.run(_run_in_copy)
    assert inside == BACKGROUND_REVIEW
    # Parent context unaffected.
    assert get_current_write_origin() == original


class TestDebiasOutcomeCredit:
    """#2898 memory-reward trap (RoMeRL arXiv:2608.02508): credit goes ONLY
    to load-bearing AND co-retrieved memories, bounded by MAX_OUTCOME_UTILITY."""

    def test_credits_only_load_bearing_and_co_retrieved(self):
        from tools.skill_provenance import debias_outcome_credit

        credit = debias_outcome_credit(
            co_retrieved=["a", "b", "c"], load_bearing=["a"], outcome_reward=1.0
        )
        assert credit == {"a": 1.0}
        assert "b" not in credit and "c" not in credit

    def test_splits_bounded_reward_across_relevant_memories(self):
        from tools.skill_provenance import debias_outcome_credit

        credit = debias_outcome_credit(
            co_retrieved=["a", "b", "c"], load_bearing=["a", "b"], outcome_reward=1.0
        )
        assert credit == {"a": 0.5, "b": 0.5}

    def test_ignores_load_bearing_not_in_retrieved_set(self):
        from tools.skill_provenance import debias_outcome_credit

        credit = debias_outcome_credit(["x"], ["a"], 1.0)
        assert credit == {}

    def test_reward_bounded_by_max_utility(self):
        from tools.skill_provenance import MAX_OUTCOME_UTILITY, debias_outcome_credit

        credit = debias_outcome_credit(["a"], ["a"], 10.0)
        assert credit["a"] == MAX_OUTCOME_UTILITY
        assert credit["a"] <= MAX_OUTCOME_UTILITY

    def test_no_load_bearing_means_no_credit(self):
        from tools.skill_provenance import debias_outcome_credit

        assert debias_outcome_credit(["a", "b"], [], 1.0) == {}

    def test_negative_reward_means_no_credit(self):
        from tools.skill_provenance import debias_outcome_credit

        assert debias_outcome_credit(["a"], ["a"], -1.0) == {}

    def test_custom_ceiling_respected(self):
        from tools.skill_provenance import debias_outcome_credit

        assert debias_outcome_credit(["a"], ["a"], 5.0, max_utility=2.0) == {"a": 2.0}


class TestRecordPromotionAttribution:
    """#2898 live promotion path persists causal attribution."""

    def _fake_mutate(self, name, apply, written):
        rec = {"name": name}
        apply(rec)
        written.update(rec)

    def _patch_mutate(self, monkeypatch, written):
        # ``record_promotion`` imports ``_mutate`` lazily from
        # ``tools.skill_usage`` inside the function body, so the patch must
        # target that consumer module, not ``skill_provenance`` itself.
        import tools.skill_usage as su

        monkeypatch.setattr(su, "_mutate", lambda n, a: self._fake_mutate(n, a, written))

    def test_attribution_and_credit_persisted(self, monkeypatch):
        from tools.skill_provenance import record_promotion

        written = {}
        self._patch_mutate(monkeypatch, written)
        record_promotion(
            "myskill",
            reason="provenance_ok",
            attribution=["read_file://a.py", "terminal://ls"],
            outcome_reward=1.0,
        )
        assert written["attribution"] == ["read_file://a.py", "terminal://ls"]
        assert written["outcome_credit"] == {"read_file://a.py": 0.5, "terminal://ls": 0.5}
        assert written["promotion_reason"] == "provenance_ok"

    def test_no_attribution_omits_keys(self, monkeypatch):
        """A fluke-success promotion with no recorded attribution must not
        fabricate one — that is what the misevolution gate (#2521) checks."""
        from tools.skill_provenance import record_promotion

        written = {}
        self._patch_mutate(monkeypatch, written)
        record_promotion("myskill", reason="provenance_ok")
        assert "attribution" not in written
        assert "outcome_credit" not in written
