"""Tests for the Agent-as-a-Judge persistent calibration memory (issue #305).

Covers the two halves of the slice independently:

* the sqlite ``CalibrationStore`` (open/record/read, malformed-row tolerance),
  always against a tmp DB path — never the real ``HERMES_HOME``;
* the pure re-weighting math (empty-store no-op, disagreement → weight nudge,
  re-eval actually changes the score) with hand-built cases, no I/O.
"""

import json
import sqlite3

import pytest

from agent.agent_judge import (
    DEFAULT_RUBRIC,
    AgentJudge,
    Rubric,
    RubricDimension,
)
from agent.judge_calibration import (
    CalibrationCase,
    CalibrationStore,
    derive_weight_adjustments,
    normalize_human_label,
    recalibrate_and_score,
    recalibrated_rubric,
)


# A trace the heuristic scores high: delivers a final answer, no failures.
GOOD_TRACE = [
    {"role": "user", "content": "list files"},
    {"role": "assistant", "content": "", "tool_calls": [{"function": {"name": "terminal"}, "id": "1"}]},
    {"role": "tool", "content": "a.py\nb.py"},
    {"role": "assistant", "content": "Two files: a.py and b.py."},
]


def _verdict_dict(overall, dim_scores, session_id="s1"):
    """A minimal JudgeVerdict.to_dict()-shaped payload."""
    return {
        "session_id": session_id,
        "overall_score": overall,
        "dimension_scores": dim_scores,
        "rationale": "x",
        "method": "heuristic",
        "model": None,
        "trace_summary": {},
    }


def _case(overall, dim_scores, human_label, session_id="s1"):
    return CalibrationCase(
        session_id=session_id,
        trace_summary={},
        judge_verdict=_verdict_dict(overall, dim_scores, session_id),
        human_label=normalize_human_label(human_label),
    )


# ── normalize_human_label ────────────────────────────────────────────────────


class TestNormalizeHumanLabel:
    @pytest.mark.parametrize("good", [True, 1, "good", "PASS", "yes", "ok", "accept"])
    def test_good(self, good):
        assert normalize_human_label(good) is True

    @pytest.mark.parametrize("bad", [False, 0, "bad", "FAIL", "no", "reject"])
    def test_bad(self, bad):
        assert normalize_human_label(bad) is False

    @pytest.mark.parametrize("unknown", [None, "maybe", 2, 0.5, object()])
    def test_unlabelled(self, unknown):
        assert normalize_human_label(unknown) is None


# ── CalibrationStore (sqlite, tmp path) ──────────────────────────────────────


class TestCalibrationStore:
    def _store(self, tmp_path):
        return CalibrationStore(db_path=tmp_path / "calib.db")

    def test_empty_store_returns_no_cases(self, tmp_path):
        store = self._store(tmp_path).open()
        assert store.cases() == []

    def test_open_is_idempotent_and_creates_file(self, tmp_path):
        store = self._store(tmp_path)
        store.open()
        store.open()  # second call must not raise / re-create
        assert (tmp_path / "calib.db").exists()

    def test_record_then_read_roundtrip(self, tmp_path):
        store = self._store(tmp_path).open()
        summary = {"message_count": 4, "has_final_answer": True}
        verdict = _verdict_dict(0.9, {"task_completion": 1.0, "tool_use": 0.8})
        case = store.record_case("sess-A", summary, verdict, human_label="good")
        assert case.human_label is True

        cases = store.cases()
        assert len(cases) == 1
        got = cases[0]
        assert got.session_id == "sess-A"
        assert got.trace_summary == summary
        assert got.judge_verdict == verdict
        assert got.human_label is True
        assert got.overall_score == pytest.approx(0.9)
        assert got.dimension_scores["task_completion"] == pytest.approx(1.0)

    def test_record_verdict_helper(self, tmp_path):
        store = self._store(tmp_path).open()
        verdict = AgentJudge().score("sess-V", GOOD_TRACE, use_llm=False)
        case = store.record_verdict(verdict, human_label=False)
        assert case.human_label is False
        cases = store.cases()
        assert len(cases) == 1
        assert cases[0].session_id == "sess-V"
        # the stored verdict payload round-trips the dimension scores
        assert set(cases[0].dimension_scores) == set(DEFAULT_RUBRIC.keys())

    def test_unrecognised_label_stored_as_null(self, tmp_path):
        store = self._store(tmp_path).open()
        store.record_case("s", {}, _verdict_dict(0.5, {}), human_label="meh")
        cases = store.cases()
        assert len(cases) == 1
        assert cases[0].human_label is None

    def test_session_filter(self, tmp_path):
        store = self._store(tmp_path).open()
        store.record_case("a", {}, _verdict_dict(0.5, {}), human_label=True)
        store.record_case("b", {}, _verdict_dict(0.5, {}), human_label=True)
        assert len(store.cases()) == 2
        assert len(store.cases(session_id="a")) == 1
        assert store.cases(session_id="a")[0].session_id == "a"

    def test_context_manager(self, tmp_path):
        with CalibrationStore(db_path=tmp_path / "ctx.db") as store:
            store.record_case("s", {}, _verdict_dict(0.5, {}), human_label=True)
            assert len(store.cases()) == 1

    def test_malformed_rows_tolerated(self, tmp_path):
        db = tmp_path / "calib.db"
        store = CalibrationStore(db_path=db).open()
        # one good row through the API
        store.record_case("ok", {"m": 1}, _verdict_dict(0.5, {"x": 0.5}), human_label=True)
        # inject a malformed row directly (un-decodable JSON columns)
        conn = sqlite3.connect(str(db))
        conn.execute(
            "INSERT INTO calibration_cases "
            "(session_id, trace_summary, judge_verdict, human_label, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("broken", "{not json", "also{bad", 1, 123.0),
        )
        conn.commit()
        conn.close()

        cases = store.cases()  # must not raise
        # only the good row survives; the malformed one is skipped
        assert len(cases) == 1
        assert cases[0].session_id == "ok"

    def test_default_db_path_honours_hermes_home(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        from agent.judge_calibration import default_db_path

        assert default_db_path() == tmp_path / "judge_calibration.db"


# ── Pure re-weighting math ───────────────────────────────────────────────────


class TestDeriveWeightAdjustments:
    def test_empty_cases_is_noop(self):
        factors = derive_weight_adjustments([], DEFAULT_RUBRIC)
        assert factors == {k: 1.0 for k in DEFAULT_RUBRIC.keys()}

    def test_agreement_case_is_noop(self):
        # judge passed (0.9 >= 0.7) AND human said good → agreement → no change
        case = _case(0.9, {k: 0.9 for k in DEFAULT_RUBRIC.keys()}, human_label="good")
        factors = derive_weight_adjustments([case], DEFAULT_RUBRIC)
        assert factors == {k: 1.0 for k in DEFAULT_RUBRIC.keys()}

    def test_unlabelled_case_is_noop(self):
        case = _case(0.9, {k: 0.9 for k in DEFAULT_RUBRIC.keys()}, human_label=None)
        factors = derive_weight_adjustments([case], DEFAULT_RUBRIC)
        assert factors == {k: 1.0 for k in DEFAULT_RUBRIC.keys()}

    def test_judge_pass_human_bad_downweights_optimistic_dim(self):
        # Judge over-trusted: overall 0.9 (pass), human said BAD.
        # task_completion scored ABOVE overall (optimistic, wrong-leaning) →
        # down-weight; tool_use scored BELOW overall (pessimistic, right) → up.
        scores = {
            "task_completion": 1.0,   # > 0.9  → optimistic → away from "bad" → DOWN
            "tool_use": 0.5,          # < 0.9  → pessimistic → toward "bad"   → UP
            "reasoning": 0.95,        # > 0.9  → optimistic → DOWN
            "efficiency": 0.6,        # < 0.9  → pessimistic → UP
        }
        case = _case(0.9, scores, human_label="bad")
        factors = derive_weight_adjustments([case], DEFAULT_RUBRIC)
        assert factors["task_completion"] < 1.0
        assert factors["reasoning"] < 1.0
        assert factors["tool_use"] > 1.0
        assert factors["efficiency"] > 1.0

    def test_judge_fail_human_good_is_symmetric(self):
        # Judge over-penalised: overall 0.4 (fail), human said GOOD.
        # A dim scored BELOW overall dragged it down wrongly → DOWN-weight;
        # a dim scored ABOVE overall leaned toward "good" → UP-weight.
        scores = {
            "task_completion": 0.1,   # < 0.4 → pessimistic → away from "good" → DOWN
            "tool_use": 0.8,          # > 0.4 → optimistic → toward "good"      → UP
            "reasoning": 0.2,         # < 0.4 → DOWN
            "efficiency": 0.9,        # > 0.4 → UP
        }
        case = _case(0.4, scores, human_label="good")
        factors = derive_weight_adjustments([case], DEFAULT_RUBRIC)
        assert factors["task_completion"] < 1.0
        assert factors["reasoning"] < 1.0
        assert factors["tool_use"] > 1.0
        assert factors["efficiency"] > 1.0

    def test_repeated_disagreement_compounds(self):
        scores = {"task_completion": 1.0, "tool_use": 0.5, "reasoning": 0.95, "efficiency": 0.6}
        one = derive_weight_adjustments([_case(0.9, scores, "bad")], DEFAULT_RUBRIC)
        two = derive_weight_adjustments(
            [_case(0.9, scores, "bad"), _case(0.9, scores, "bad")], DEFAULT_RUBRIC
        )
        # two disagreements push the optimistic dim further down than one
        assert two["task_completion"] < one["task_completion"]


class TestRecalibratedRubric:
    def test_no_cases_preserves_weights_exactly(self):
        new = recalibrated_rubric(DEFAULT_RUBRIC, [])
        before = {d.key: d.weight for d in DEFAULT_RUBRIC.dimensions}
        after = {d.key: d.weight for d in new.dimensions}
        assert after == before

    def test_returns_new_object_inputs_unmutated(self):
        before = {d.key: d.weight for d in DEFAULT_RUBRIC.dimensions}
        case = _case(0.9, {k: 1.0 for k in DEFAULT_RUBRIC.keys()}, "bad")
        recalibrated_rubric(DEFAULT_RUBRIC, [case])
        after = {d.key: d.weight for d in DEFAULT_RUBRIC.dimensions}
        assert after == before  # original rubric untouched

    def test_disagreement_changes_a_weight(self):
        scores = {"task_completion": 1.0, "tool_use": 0.5, "reasoning": 0.95, "efficiency": 0.6}
        case = _case(0.9, scores, human_label="bad")
        new = recalibrated_rubric(DEFAULT_RUBRIC, [case])
        weights = {d.key: d.weight for d in new.dimensions}
        # task_completion was 2.0 in the default rubric; it must have dropped
        assert weights["task_completion"] < 2.0

    def test_weights_never_drop_to_zero(self):
        # 50 disagreements all hammering the same dim down must still floor > 0
        scores = {"task_completion": 1.0, "tool_use": 0.5, "reasoning": 0.95, "efficiency": 0.6}
        cases = [_case(0.9, scores, "bad") for _ in range(50)]
        new = recalibrated_rubric(DEFAULT_RUBRIC, cases)
        weights = {d.key: d.weight for d in new.dimensions}
        assert all(w > 0 for w in weights.values())
        # rubric is still constructible & has a positive total weight
        assert new.total_weight() > 0


# ── End-to-end re-evaluation through AgentJudge ──────────────────────────────


class TestRecalibrateAndScore:
    def test_no_cases_matches_plain_score(self):
        baseline = AgentJudge().score("s", GOOD_TRACE, use_llm=False)
        recal = recalibrate_and_score("s", GOOD_TRACE, [], use_llm=False)
        assert recal.overall_score == pytest.approx(baseline.overall_score)
        assert recal.dimension_scores == baseline.dimension_scores

    def test_calibration_changes_the_overall_score(self):
        # Build a disagreement history that re-weights the rubric, then confirm
        # re-scoring the SAME trace yields a DIFFERENT overall score.
        baseline = AgentJudge().score("s", GOOD_TRACE, use_llm=False)
        # GOOD_TRACE heuristically scores task_completion high and (say)
        # reasoning lower; a disagreement that down-weights the high dims and
        # up-weights the low ones shifts the weighted mean.
        scores = {"task_completion": 1.0, "tool_use": 0.5, "reasoning": 0.95, "efficiency": 0.6}
        cases = [_case(0.9, scores, human_label="bad") for _ in range(3)]
        recal = recalibrate_and_score("s", GOOD_TRACE, cases, use_llm=False)
        assert recal.overall_score != pytest.approx(baseline.overall_score)
        # per-dimension scores are unchanged (same heuristic); only the
        # weighted aggregation moved
        assert recal.dimension_scores == baseline.dimension_scores

    def test_respects_custom_base_rubric(self):
        rubric = Rubric(
            dimensions=(
                RubricDimension("a", "A", "desc a", weight=1.0),
                RubricDimension("b", "B", "desc b", weight=1.0),
            )
        )
        verdict = recalibrate_and_score(
            "s", GOOD_TRACE, [], base_rubric=rubric, use_llm=False
        )
        assert set(verdict.dimension_scores) == {"a", "b"}


# ── Store → math integration ─────────────────────────────────────────────────


def test_store_to_recalibration_pipeline(tmp_path):
    """Record cases, read them back, recalibrate, re-score — the full slice."""
    store = CalibrationStore(db_path=tmp_path / "pipe.db").open()
    baseline = AgentJudge().score("s", GOOD_TRACE, use_llm=False)

    # record three disagreement cases (judge passed, human said bad)
    scores = {"task_completion": 1.0, "tool_use": 0.5, "reasoning": 0.95, "efficiency": 0.6}
    for _ in range(3):
        store.record_case("s", {}, _verdict_dict(0.9, scores), human_label="bad")

    cases = store.cases()
    assert len(cases) == 3
    recal = recalibrate_and_score("s", GOOD_TRACE, cases, use_llm=False)
    assert recal.overall_score != pytest.approx(baseline.overall_score)
