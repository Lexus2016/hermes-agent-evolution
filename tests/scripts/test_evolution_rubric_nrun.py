"""Tests for N-run reliability measurement in the evolution rubric (#50).

A single rubric score cannot distinguish a consistently good cycle from a
lucky one. These tests cover the reliability statistics (mean / stddev /
pass@k math on deterministic fixture scores), the unreliable-flag threshold
behavior, n_runs config validation (out-of-range rejected, non-int handled),
and the n_runs=1 backward-compatibility contract.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from evolution_rubric_judge import (
    RUBRIC_RELIABILITY_DEFAULTS,
    RUBRIC_RELIABILITY_MAX_RUNS,
    RUBRIC_RELIABILITY_MIN_RUNS,
    StrictRubricJudgeGrader,
    _parse_n_runs,
    aggregate_scorecards,
    compute_reliability,
    load_rubric_config,
    run_scored_evaluations,
)


# ── mean / stddev / pass@k math on deterministic fixture scores ───────


def test_compute_reliability_math_on_fixture_scores() -> None:
    # [90, 50, 40]: mean 60.0; population stddev = sqrt((900+100+400)/3) ≈ 21.6
    rel = compute_reliability(
        [90.0, 50.0, 40.0],
        pass_at_k=1,
        success_threshold=50.0,
        variance_threshold=10.0,
    )
    assert rel["n_runs"] == 3
    assert rel["mean_score"] == 60.0
    assert rel["stddev"] == pytest.approx(21.6, abs=0.05)
    # 2 of 3 runs succeed (>= 50) → p = 2/3; pass@1 = 1 - (1/3)^3 = 26/27
    assert rel["pass_at_k"] == pytest.approx(26 / 27, abs=0.001)
    assert rel["unreliable"] is True


def test_compute_reliability_pass_at_k_general_k() -> None:
    # Same fixture, pass@2: P(X >= 2) for X ~ Binomial(3, 2/3) = 20/27
    rel = compute_reliability([90.0, 50.0, 40.0], pass_at_k=2, success_threshold=50.0)
    assert rel["pass_at_k"] == pytest.approx(20 / 27, abs=0.001)


def test_compute_reliability_constant_scores_zero_stddev() -> None:
    rel = compute_reliability([70.0, 70.0, 70.0], pass_at_k=1, success_threshold=50.0)
    assert rel["mean_score"] == 70.0
    assert rel["stddev"] == 0.0
    assert rel["pass_at_k"] == 1.0  # every run succeeds
    assert rel["unreliable"] is False


def test_compute_reliability_population_stddev_not_sample() -> None:
    # Population stddev of [0, 100] is 50.0 (divide by n=2), not 70.71
    # (sample / (n-1)).
    rel = compute_reliability([0.0, 100.0], pass_at_k=1, success_threshold=50.0)
    assert rel["mean_score"] == 50.0
    assert rel["stddev"] == 50.0
    assert rel["pass_at_k"] == pytest.approx(0.75, abs=0.001)  # 1 - (1/2)^2


def test_compute_reliability_single_run() -> None:
    rel = compute_reliability([64.2], pass_at_k=1, success_threshold=50.0)
    assert rel["mean_score"] == 64.2
    assert rel["stddev"] == 0.0
    assert rel["pass_at_k"] == 1.0
    assert rel["unreliable"] is False


def test_compute_reliability_empty_scores() -> None:
    rel = compute_reliability([])
    assert rel == {
        "n_runs": 0,
        "mean_score": 0.0,
        "stddev": 0.0,
        "pass_at_k": 0.0,
        "unreliable": False,
    }


# ── unreliable-flag threshold behavior ────────────────────────────────


def test_unreliable_flag_threshold_behavior() -> None:
    scores = [90.0, 50.0, 40.0]  # stddev ≈ 21.6
    assert compute_reliability(scores, variance_threshold=10.0)["unreliable"] is True
    assert compute_reliability(scores, variance_threshold=21.5)["unreliable"] is True
    assert compute_reliability(scores, variance_threshold=21.7)["unreliable"] is False
    assert compute_reliability(scores, variance_threshold=100.0)["unreliable"] is False


def test_aggregate_scorecards_adds_unreliable_flag() -> None:
    base = {
        "cycle_date": "2026-06-23",
        "grader": "strict",
        "overall_percentage": 90.0,
        "total_score": 46.8,
        "total_max": 52.0,
        "flags": [],
    }
    runs = [dict(base, overall_percentage=p) for p in (90.0, 50.0, 40.0)]
    config = dict(RUBRIC_RELIABILITY_DEFAULTS, variance_threshold=10.0)
    out = aggregate_scorecards(runs, config)
    assert out["unreliable"] is True
    assert any(f.startswith("UNRELIABLE:") for f in out["flags"])
    assert any("21.6" in f for f in out["flags"])
    # Headline numbers become the mean across runs
    assert out["overall_percentage"] == 60.0
    assert out["total_score"] == round(52.0 * 0.6, 1)
    assert out["n_runs"] == 3
    assert out["mean_score"] == 60.0
    assert out["run_scores"] == [90.0, 50.0, 40.0]


def test_aggregate_scorecards_within_threshold_no_flag() -> None:
    base = {
        "cycle_date": "2026-06-23",
        "grader": "strict",
        "overall_percentage": 90.0,
        "total_score": 46.8,
        "total_max": 52.0,
        "flags": ["MODERATE: room for improvement"],
    }
    runs = [dict(base, overall_percentage=p) for p in (90.0, 85.0, 80.0)]
    config = dict(RUBRIC_RELIABILITY_DEFAULTS, variance_threshold=10.0)
    out = aggregate_scorecards(runs, config)
    assert out["unreliable"] is False
    assert out["flags"] == ["MODERATE: room for improvement"]  # untouched


# ── n_runs config validation ──────────────────────────────────────────


@pytest.mark.parametrize(
    "bad",
    [0, -1, 11, 100, "abc", 3.5, True, None, [3], {"n": 3}],
)
def test_parse_n_runs_rejects_invalid_values(bad: object) -> None:
    assert _parse_n_runs(bad) == RUBRIC_RELIABILITY_DEFAULTS["n_runs"]
    assert _parse_n_runs(bad, default=1) == 1  # explicit default honored


@pytest.mark.parametrize("good", [1, 2, 3, 10])
def test_parse_n_runs_accepts_valid_values(good: int) -> None:
    assert _parse_n_runs(good) == good
    assert _parse_n_runs(str(good)) == good  # int-convertible strings accepted


def test_n_runs_bound_constants_match_spec() -> None:
    assert RUBRIC_RELIABILITY_MIN_RUNS == 1  # legacy single-run mode
    assert RUBRIC_RELIABILITY_MAX_RUNS == 10  # cost bound cap
    assert RUBRIC_RELIABILITY_DEFAULTS["n_runs"] == 3


def test_load_rubric_config_applies_valid_yaml(tmp_path: Path) -> None:
    cfg_file = tmp_path / "rubric-judge.yaml"
    cfg_file.write_text(
        "reliability:\n"
        "  n_runs: 5\n"
        "  temperature: 0.9\n"
        "  variance_threshold: 7.5\n"
        "  pass_at_k: 2\n"
        "  success_threshold: 60\n",
        encoding="utf-8",
    )
    cfg = load_rubric_config(cfg_file)
    assert cfg["n_runs"] == 5
    assert cfg["temperature"] == 0.9
    assert cfg["variance_threshold"] == 7.5
    assert cfg["pass_at_k"] == 2
    assert cfg["success_threshold"] == 60.0


def test_load_rubric_config_rejects_out_of_range_and_non_int(tmp_path: Path) -> None:
    cfg_file = tmp_path / "rubric-judge.yaml"
    cfg_file.write_text(
        "reliability:\n"
        "  n_runs: 42\n"
        "  temperature: 0\n"
        "  variance_threshold: -1\n"
        "  pass_at_k: 0\n",
        encoding="utf-8",
    )
    cfg = load_rubric_config(cfg_file)
    assert cfg["n_runs"] == 3  # 42 out of range → default
    assert cfg["temperature"] == 0.7  # non-positive → default
    assert cfg["variance_threshold"] == 10.0  # negative → default
    assert cfg["pass_at_k"] == 1  # 0 invalid → default

    cfg_file.write_text("reliability:\n  n_runs: not-a-number\n", encoding="utf-8")
    assert load_rubric_config(cfg_file)["n_runs"] == 3


def test_load_rubric_config_missing_file_uses_defaults(tmp_path: Path) -> None:
    assert load_rubric_config(tmp_path / "does-not-exist.yaml") == dict(
        RUBRIC_RELIABILITY_DEFAULTS
    )


def test_load_rubric_config_missing_section_uses_defaults(tmp_path: Path) -> None:
    cfg_file = tmp_path / "rubric-judge.yaml"
    cfg_file.write_text(
        'name: evolution-rubric-judge\nschedule: "45 7 * * *"\n', encoding="utf-8"
    )
    assert load_rubric_config(cfg_file) == dict(RUBRIC_RELIABILITY_DEFAULTS)


def test_load_rubric_config_shipped_yaml_is_valid() -> None:
    """The shipped cron yaml must parse and carry an in-range n_runs."""
    cfg = load_rubric_config()
    assert RUBRIC_RELIABILITY_MIN_RUNS <= cfg["n_runs"] <= RUBRIC_RELIABILITY_MAX_RUNS
    assert cfg["temperature"] > 0


# ── backward compatibility at n_runs=1 ────────────────────────────────


def test_aggregate_scorecards_n_runs_one_preserves_legacy_fields() -> None:
    base = {
        "cycle_date": "2026-06-23",
        "grader": "strict",
        "dimensions": {"research": {"score": 7.0, "max": 10.0}},
        "total_score": 33.4,
        "total_max": 52.0,
        "overall_percentage": 64.2,
        "flags": ["MODERATE: overall quality < 70% — room for improvement"],
    }
    config = dict(RUBRIC_RELIABILITY_DEFAULTS, n_runs=1)
    out = aggregate_scorecards([base], config)
    # Pre-#50 fields keep today's exact values
    assert out["overall_percentage"] == 64.2
    assert out["total_score"] == 33.4
    assert out["total_max"] == 52.0
    assert out["flags"] == base["flags"]
    assert out["dimensions"] == base["dimensions"]
    # Inert reliability fields
    assert out["n_runs"] == 1
    assert out["mean_score"] == 64.2
    assert out["stddev"] == 0.0
    assert out["pass_at_k"] == 1.0
    assert out["unreliable"] is False
    assert out["run_scores"] == [64.2]


def _stage_research(tmp_path: Path) -> None:
    (tmp_path / "research").mkdir(parents=True, exist_ok=True)
    (tmp_path / "research" / "2026-06-23.md").write_text(
        "# Finding 1\n"
        "Adopting the parallel compactor improved merge latency by 52% across "
        "the benchmark suite. Source: https://example.com/bench.\n",
        encoding="utf-8",
    )


def test_strict_grader_n_runs_one_matches_direct_score(tmp_path: Path) -> None:
    _stage_research(tmp_path)
    direct = StrictRubricJudgeGrader().score("2026-06-23", tmp_path)
    runs = run_scored_evaluations(
        StrictRubricJudgeGrader(), "2026-06-23", tmp_path, n_runs=1, temperature=0.7
    )
    out = aggregate_scorecards(runs, dict(RUBRIC_RELIABILITY_DEFAULTS, n_runs=1))
    for key in (
        "cycle_date",
        "grader",
        "total_score",
        "total_max",
        "overall_percentage",
        "flags",
    ):
        assert out[key] == direct[key]
    assert out["n_runs"] == 1
    assert out["stddev"] == 0.0


def test_strict_grader_n_runs_three_deterministic_runs(tmp_path: Path) -> None:
    """The shipped grader is deterministic: N runs are identical, so the
    reliability fields attest consistency (stddev 0.0, never unreliable)."""
    _stage_research(tmp_path)
    runs = run_scored_evaluations(
        StrictRubricJudgeGrader(), "2026-06-23", tmp_path, n_runs=3, temperature=0.7
    )
    assert len(runs) == 3
    assert len({r["overall_percentage"] for r in runs}) == 1
    out = aggregate_scorecards(runs, dict(RUBRIC_RELIABILITY_DEFAULTS))
    assert out["n_runs"] == 3
    assert out["stddev"] == 0.0
    assert out["unreliable"] is False
    assert out["mean_score"] == out["overall_percentage"]
    assert out["run_scores"] == [out["overall_percentage"]] * 3


# ── temperature plumbing for stochastic graders ───────────────────────


class _TemperatureRecordingGrader:
    """Fake LLM-backed grader whose score() accepts a temperature kwarg."""

    def __init__(self) -> None:
        self.seen_temperatures: list[float] = []

    def score(self, date: str, evolution_dir: Path, temperature: float = 0.0) -> dict:
        self.seen_temperatures.append(temperature)
        return {
            "cycle_date": date,
            "grader": "fake",
            "overall_percentage": 40.0 + len(self.seen_temperatures) * 10.0,
            "total_score": 20.0,
            "total_max": 52.0,
            "flags": [],
        }


class _TemperatureIgnoringGrader:
    """Fake grader without a temperature kwarg — must not break the runner."""

    def score(self, date: str, evolution_dir: Path) -> dict:
        return {
            "cycle_date": date,
            "grader": "fake",
            "overall_percentage": 55.0,
            "total_score": 28.6,
            "total_max": 52.0,
            "flags": [],
        }


def test_run_scored_evaluations_passes_temperature_to_supporting_grader(
    tmp_path: Path,
) -> None:
    grader = _TemperatureRecordingGrader()
    runs = run_scored_evaluations(
        grader, "2026-06-23", tmp_path, n_runs=3, temperature=0.7
    )
    assert grader.seen_temperatures == [0.7, 0.7, 0.7]
    assert [r["overall_percentage"] for r in runs] == [50.0, 60.0, 70.0]


def test_run_scored_evaluations_temperature_ignored_when_unsupported(
    tmp_path: Path,
) -> None:
    grader = _TemperatureIgnoringGrader()
    runs = run_scored_evaluations(
        grader, "2026-06-23", tmp_path, n_runs=2, temperature=0.7
    )
    assert len(runs) == 2
    assert all(r["overall_percentage"] == 55.0 for r in runs)


def test_run_scored_evaluations_aggregates_stochastic_grader(tmp_path: Path) -> None:
    grader = _TemperatureRecordingGrader()
    runs = run_scored_evaluations(
        grader, "2026-06-23", tmp_path, n_runs=3, temperature=0.7
    )
    out = aggregate_scorecards(
        runs,
        dict(
            RUBRIC_RELIABILITY_DEFAULTS,
            success_threshold=60.0,
            variance_threshold=5.0,
        ),
    )
    # Scores [50, 60, 70]: mean 60.0, stddev ≈ 8.16, 2/3 succeed
    assert out["mean_score"] == 60.0
    assert out["stddev"] == pytest.approx(8.2, abs=0.05)
    assert out["pass_at_k"] == pytest.approx(1 - (1 / 3) ** 3, abs=0.001)
    assert out["unreliable"] is True


# ── end-to-end: main() writes reliability fields to the scorecard ─────


def test_main_scores_with_reliability_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from evolution_rubric_judge import main

    _stage_research(tmp_path)
    monkeypatch.setenv("EVOLUTION_PROFILE_DIR", str(tmp_path))
    cfg_file = tmp_path / "rubric-judge.yaml"
    cfg_file.write_text(
        "reliability:\n  n_runs: 3\n  variance_threshold: 10.0\n",
        encoding="utf-8",
    )
    assert main(["--score", "2026-06-23", "--config", str(cfg_file)]) == 0

    records = json.loads(
        (tmp_path / "rubric-scorecard.jsonl").read_text(encoding="utf-8")
    )
    assert records["n_runs"] == 3
    assert "mean_score" in records
    assert "stddev" in records
    assert "pass_at_k" in records
    assert "unreliable" in records
    assert "run_scores" in records
    assert len(records["run_scores"]) == 3
    out = capsys.readouterr().out
    assert "n_runs=3" in out
    assert "stddev=0.0" in out
