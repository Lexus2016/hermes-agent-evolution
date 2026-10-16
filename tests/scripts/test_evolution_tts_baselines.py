"""Tests for scripts/evolution_tts_baselines.py — naive test-time-scaling
baseline arms for evolution evaluation (issue #41)."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from evolution_tts_baselines import (
    baseline_curve,
    best_of_n,
    compare_against_baseline,
    main,
    pass_at_k,
)


class TestBestOfN:
    def test_max_of_samples(self):
        assert best_of_n([0.4, 0.7, 0.9]) == 0.9

    def test_empty_is_zero(self):
        assert best_of_n([]) == 0.0

    def test_single_sample_is_itself(self):
        assert best_of_n([0.5]) == 0.5


class TestPassAtK:
    def test_all_pass_is_one(self):
        assert pass_at_k([0.8, 0.9, 0.95], threshold=0.75) == 1.0

    def test_none_pass_is_zero(self):
        assert pass_at_k([0.1, 0.2], threshold=0.75) == 0.0

    def test_partial_pass_matches_formula(self):
        # 2 of 4 pass -> p=0.5 -> 1 - 0.5^4 = 0.9375
        got = pass_at_k([0.9, 0.9, 0.1, 0.1], threshold=0.75)
        assert abs(got - 0.9375) < 1e-9

    def test_empty_is_zero(self):
        assert pass_at_k([], threshold=0.75) == 0.0


class TestBaselineCurve:
    def test_curve_is_prefix_max(self):
        curve = baseline_curve([0.3, 0.8, 0.6], max_n=8)
        assert curve == [
            {"n": 1, "best": 0.8},
            {"n": 2, "best": 0.8},
            {"n": 3, "best": 0.8},
        ]

    def test_curve_respects_max_n(self):
        curve = baseline_curve([0.9, 0.8, 0.7], max_n=2)
        assert [c["n"] for c in curve] == [1, 2]

    def test_curve_monotone(self):
        curve = baseline_curve([0.2, 0.9, 0.4, 0.7], max_n=8)
        bests = [c["best"] for c in curve]
        assert bests == sorted(bests)  # non-decreasing


class TestCompareAgainstBaseline:
    def test_beats_baseline(self):
        r = compare_against_baseline(0.95, [0.6, 0.7, 0.8], threshold=0.75)
        assert r["verdict"] == "BEATS_BASELINE"
        assert r["best_of_n"] == 0.8
        assert r["n_samples"] == 3

    def test_ties_baseline_within_margin(self):
        # evolved 0.81 vs best-of 0.80 — inside the default 0.02 margin.
        r = compare_against_baseline(0.81, [0.6, 0.8], threshold=0.75)
        assert r["verdict"] == "TIES_BASELINE"

    def test_loses_to_baseline(self):
        r = compare_against_baseline(0.5, [0.8, 0.9], threshold=0.75)
        assert r["verdict"] == "LOSES_TO_BASELINE"

    def test_no_samples_means_no_comparison(self):
        r = compare_against_baseline(0.9, [], threshold=0.75)
        assert r["verdict"] == "NO_BASELINE"
        assert r["curve"] == []

    def test_margin_is_respected(self):
        # Evolved 0.83 vs best 0.80: beats with margin 0.01, ties with 0.05.
        assert (
            compare_against_baseline(0.83, [0.8], margin=0.01)["verdict"]
            == "BEATS_BASELINE"
        )
        assert (
            compare_against_baseline(0.83, [0.8], margin=0.05)["verdict"]
            == "TIES_BASELINE"
        )

    def test_string_samples_coerced(self):
        r = compare_against_baseline(0.9, "0.5 0.6 0.7", threshold=0.75)
        assert r["n_samples"] == 3


class TestMainCli:
    def test_cli_verdict_json(self, capsys):
        code = main([
            "evolution_tts_baselines.py",
            "--evolved",
            "0.95",
            "--sample",
            "0.6",
            "--sample",
            "0.8",
        ])
        out = json.loads(capsys.readouterr().out)
        assert code == 0
        assert out["verdict"] == "BEATS_BASELINE"
        assert out["n_samples"] == 2

    def test_cli_requires_evolved(self, capsys):
        code = main(["evolution_tts_baselines.py", "--threshold", "0.5"])
        err = capsys.readouterr().err
        assert code == 2
        assert "--evolved" in err

    def test_cli_reads_path(self, capsys, tmp_path):
        p = tmp_path / "scores.json"
        p.write_text(json.dumps([0.4, 0.7, 0.9]), encoding="utf-8")
        code = main(["evolution_tts_baselines.py", "--evolved", "0.95", str(p)])
        out = json.loads(capsys.readouterr().out)
        assert code == 0
        assert out["n_samples"] == 3

    def test_cli_reads_stdin(self, capsys, monkeypatch):
        monkeypatch.setattr(
            "sys.stdin", type("FakeStdin", (), {"read": lambda self: "[0.5, 0.9]"})()
        )
        code = main(["evolution_tts_baselines.py", "--evolved", "0.95"])
        out = json.loads(capsys.readouterr().out)
        assert code == 0
        assert out["best_of_n"] == 0.9
