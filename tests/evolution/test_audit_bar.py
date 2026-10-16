"""Tests for the non-stationary audit bar (issue #63).

Covers the four required behaviors: calibration-trap recognition (known
observations are marked accepted and never re-reported), new-drift detection
still firing alongside traps, miss-threshold crossing triggering rubric
rotation, and state persistence round-trip (fail-open).
"""

import sys
from datetime import datetime, timezone, UTC
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from evolution.lib.audit_bar import (
    AUDIT_RUBRIC_VARIANTS,
    AuditBarState,
    CHANGED,
    DEFAULT_MISS_THRESHOLD,
    NEW,
    TRAP,
    active_rubric,
    build_audit_prompt,
    classify_observation,
    default_state,
    find_missing,
    find_new_drift,
    load_state,
    observation_id,
    rotate_rubric,
    save_state,
    state_file_path,
    state_from_dict,
    state_to_dict,
    traps_in,
    update_bar_state,
)


def _obs(name, verdict="keep", utility=5.0, share=0.3):
    return {
        "kind": "skill",
        "name": name,
        "verdict": verdict,
        "utility": utility,
        "share": share,
    }


def _now():
    return datetime(2026, 8, 19, 6, 40, tzinfo=UTC)


class TestTrapRecognition:
    def test_known_observation_is_classified_as_trap(self):
        accepted = [_obs("workhorse", share=0.6)]
        assert classify_observation(_obs("workhorse", share=0.6), accepted) == TRAP
        assert traps_in([_obs("workhorse", share=0.6)], accepted) == [
            _obs("workhorse", share=0.6)
        ]

    def test_prompt_marks_traps_known_accepted(self):
        accepted = [_obs("workhorse", share=0.6), _obs("niche", verdict="demote")]
        prompt = build_audit_prompt(
            AUDIT_RUBRIC_VARIANTS, 0, accepted, DEFAULT_MISS_THRESHOLD
        )
        assert "CALIBRATION TRAPS" in prompt
        assert "KNOWN/ACCEPTED" in prompt
        assert "already known/accepted" in prompt
        assert "workhorse" in prompt
        assert "niche" in prompt
        # The prompt must tell the auditor traps are known and to still find
        # NEW drift — not to stop looking.
        assert "never report" in prompt
        assert "NEW drift" in prompt
        # The active checklist variant is embedded in the prompt.
        assert "CORPUS COVERAGE" in prompt

    def test_trap_never_appears_in_new_drift(self):
        accepted = [_obs("workhorse", share=0.6)]
        drift = find_new_drift([_obs("workhorse", share=0.6)], accepted)
        assert drift == []

    def test_identity_ignores_irrelevant_fields(self):
        accepted = [_obs("workhorse", share=0.6)]
        # Same id + same content fields -> trap regardless of list shape.
        assert (
            classify_observation(
                {
                    "kind": "skill",
                    "name": "workhorse",
                    "verdict": "keep",
                    "utility": 5.0,
                    "share": 0.6,
                },
                accepted,
            )
            == TRAP
        )


class TestNewDriftDetection:
    def test_new_item_is_drift(self):
        accepted = [_obs("workhorse")]
        assert (
            classify_observation(_obs("brand-new", verdict="remove"), accepted) == NEW
        )
        drift = find_new_drift([_obs("brand-new", verdict="remove")], accepted)
        assert [observation_id(o) for o in drift] == ["skill:brand-new"]

    def test_changed_content_on_known_item_is_drift(self):
        accepted = [_obs("workhorse", verdict="keep", share=0.6)]
        changed = _obs("workhorse", verdict="demote", share=0.02)
        assert classify_observation(changed, accepted) == CHANGED
        assert find_new_drift([changed], accepted) == [changed]

    def test_drift_fires_alongside_traps(self):
        accepted = [_obs("workhorse", share=0.6), _obs("minor", verdict="demote")]
        observations = [
            _obs("workhorse", share=0.6),  # trap — unchanged
            _obs("minor", verdict="remove"),  # changed — drift
            _obs("brand-new", verdict="remove"),  # new — drift
        ]
        drift = find_new_drift(observations, accepted)
        assert [observation_id(o) for o in drift] == [
            "skill:minor",
            "skill:brand-new",
        ]

    def test_disappeared_accepted_observation_is_drift(self):
        accepted = [_obs("workhorse"), _obs("vanished", verdict="demote")]
        missing = find_missing([_obs("workhorse")], accepted)
        assert [observation_id(o) for o in missing] == ["skill:vanished"]


class TestMissCountingAndRotation:
    def test_miss_count_increments_on_unreported_drift(self):
        state = default_state()
        new_state, events = update_bar_state(
            state,
            drift_occurred=True,
            drift_reported=False,
            observations=[_obs("x")],
            now=_now(),
        )
        assert new_state.miss_count == 1
        assert any("MISS" in e for e in events)

    def test_reported_drift_resets_miss_count(self):
        state = AuditBarState(miss_count=1, miss_threshold=2)
        new_state, events = update_bar_state(
            state,
            drift_occurred=True,
            drift_reported=True,
            observations=[_obs("x")],
            now=_now(),
        )
        assert new_state.miss_count == 0
        assert any("reset" in e for e in events)

    def test_no_drift_leaves_miss_count_unchanged(self):
        state = AuditBarState(miss_count=1, miss_threshold=2)
        new_state, _ = update_bar_state(
            state,
            drift_occurred=False,
            drift_reported=False,
            observations=[_obs("x")],
            now=_now(),
        )
        assert new_state.miss_count == 1

    def test_threshold_crossing_triggers_rubric_rotation_and_resets(self):
        state = AuditBarState(miss_count=1, miss_threshold=2, rubric_variant=0)
        new_state, events = update_bar_state(
            state,
            drift_occurred=True,
            drift_reported=False,
            observations=[_obs("x")],
            now=_now(),
        )
        # Miss 2/2 >= threshold 2 -> rotated to variant 2, counter reset.
        assert new_state.rubric_variant == 1
        assert new_state.miss_count == 0
        assert len(new_state.rotations) == 1
        assert new_state.rotations[0]["from"] == 0
        assert new_state.rotations[0]["to"] == 1
        assert any("rotated" in e for e in events)

    def test_rotation_wraps_around(self):
        state = AuditBarState(
            miss_count=1,
            miss_threshold=2,
            rubric_variant=len(AUDIT_RUBRIC_VARIANTS) - 1,
        )
        new_state, _ = update_bar_state(
            state,
            drift_occurred=True,
            drift_reported=False,
            observations=[_obs("x")],
            now=_now(),
        )
        assert new_state.rubric_variant == 0

    def test_threshold_is_configurable(self):
        state = AuditBarState(miss_count=4, miss_threshold=5)
        new_state, events = update_bar_state(
            state,
            drift_occurred=True,
            drift_reported=False,
            observations=[_obs("x")],
            now=_now(),
        )
        assert new_state.miss_count == 0
        assert new_state.rubric_variant == 1
        assert any("rotated" in e for e in events)

    def test_threshold_override_persists(self):
        state = default_state()
        new_state, _ = update_bar_state(
            state,
            drift_occurred=False,
            drift_reported=False,
            observations=[_obs("x")],
            miss_threshold=3,
            now=_now(),
        )
        assert new_state.miss_threshold == 3

    def test_rotate_rubric_is_pure(self):
        state = default_state()
        rotated = rotate_rubric(state, now=_now())
        assert state.rubric_variant == 0  # input untouched
        assert rotated.rubric_variant == 1


class TestAcceptedObservationsCarryForward:
    def test_first_run_accepts_baseline(self):
        state = default_state()
        new_state, _ = update_bar_state(
            state,
            drift_occurred=False,
            drift_reported=False,
            observations=[_obs("a"), _obs("b")],
            now=_now(),
        )
        assert len(new_state.accepted_observations) == 2

    def test_unchanged_corpus_produces_no_drift_next_run(self):
        state = default_state()
        state, _ = update_bar_state(
            state,
            drift_occurred=False,
            drift_reported=False,
            observations=[_obs("a"), _obs("b")],
            now=_now(),
        )
        again, _ = update_bar_state(
            state,
            drift_occurred=False,
            drift_reported=False,
            observations=[_obs("a"), _obs("b")],
            now=_now(),
        )
        assert again.accepted_observations == state.accepted_observations

    def test_drift_observed_becomes_accepted_baseline(self):
        state = default_state()
        state, _ = update_bar_state(
            state,
            drift_occurred=False,
            drift_reported=False,
            observations=[_obs("a", verdict="keep")],
            now=_now(),
        )
        # Next run: "a" collapses to demote -> drift, and the new state
        # (demote) becomes the accepted baseline for the run after.
        state, _ = update_bar_state(
            state,
            drift_occurred=True,
            drift_reported=True,
            observations=[_obs("a", verdict="demote")],
            now=_now(),
        )
        assert [o["verdict"] for o in state.accepted_observations] == ["demote"]
        # The run after that sees the demoted state as the accepted baseline.
        again, _ = update_bar_state(
            state,
            drift_occurred=False,
            drift_reported=False,
            observations=[_obs("a", verdict="demote")],
            now=_now(),
        )
        assert again.miss_count == 0
        assert (
            find_new_drift([_obs("a", verdict="demote")], again.accepted_observations)
            == []
        )


class TestStatePersistence:
    def test_round_trip(self, tmp_path):
        state = AuditBarState(
            accepted_observations=[_obs("a"), _obs("b", verdict="demote")],
            miss_count=1,
            rubric_variant=2,
            miss_threshold=3,
            last_run_at=_now().isoformat(),
            rotations=[{"at": _now().isoformat(), "from": 1, "to": 2}],
        )
        path = state_file_path(tmp_path)
        assert path == tmp_path / "audit-bar-state.json"
        assert save_state(path, state) is True
        loaded = load_state(path)
        assert loaded.accepted_observations == state.accepted_observations
        assert loaded.miss_count == state.miss_count
        assert loaded.rubric_variant == state.rubric_variant
        assert loaded.miss_threshold == state.miss_threshold
        assert loaded.last_run_at == state.last_run_at
        assert loaded.rotations == state.rotations
        # dict round-trip matches the file shape.
        assert state_from_dict(state_to_dict(state)) == loaded

    def test_missing_file_is_fail_open_default(self, tmp_path):
        loaded = load_state(tmp_path / "nope" / "audit-bar-state.json")
        assert loaded == default_state()
        assert loaded.miss_count == 0
        assert loaded.miss_threshold == DEFAULT_MISS_THRESHOLD

    def test_corrupt_file_is_fail_open_default(self, tmp_path):
        path = tmp_path / "audit-bar-state.json"
        path.write_text("{ not json", encoding="utf-8")
        assert load_state(path) == default_state()

    def test_tolerant_decode_of_partial_state(self, tmp_path):
        path = tmp_path / "audit-bar-state.json"
        path.write_text(
            '{"miss_count": "2", "rubric_variant": 1, '
            '"accepted_observations": [{"kind": "skill", "name": "a"}]}',
            encoding="utf-8",
        )
        loaded = load_state(path)
        assert loaded.miss_count == 2
        assert loaded.rubric_variant == 1
        assert len(loaded.accepted_observations) == 1
        assert loaded.miss_threshold == DEFAULT_MISS_THRESHOLD

    def test_active_rubric_clamps_out_of_range(self):
        assert "CORPUS COVERAGE" in active_rubric(AUDIT_RUBRIC_VARIANTS, 99)
        assert "BASELINE DEVIATION" in active_rubric(AUDIT_RUBRIC_VARIANTS, 1)
