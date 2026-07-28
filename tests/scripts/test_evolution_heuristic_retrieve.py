# -*- coding: utf-8 -*-
"""Tests for judge-reranked heuristic retrieval (issue #1360, Child B of #1303)."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from evolution_heuristic_retrieve import (  # noqa: E402
    format_for_injection,
    load_heuristics,
    main,
    retrieve,
)


def _h(pattern, task_type="coding", outcome=1.0, frequency=4, text=None):
    return {
        "task_type": task_type,
        "pattern": list(pattern),
        "text": text or f"On {task_type} tasks, {pattern[0]} then {pattern[1]}.",
        "frequency": frequency,
        "outcome_score": outcome,
    }


def _store(evolution_dir, heuristics, date="2026-07-28"):
    d = evolution_dir / "heuristics"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{date}.json").write_text(
        json.dumps({"date": date, "count": len(heuristics), "heuristics": heuristics}),
        encoding="utf-8",
    )


class TestLoadHeuristics:
    def test_loads_the_newest_file(self, tmp_path):
        _store(tmp_path, [_h(["a", "b"])], date="2026-07-01")
        _store(tmp_path, [_h(["c", "d"]), _h(["e", "f"])], date="2026-07-28")
        assert len(load_heuristics(tmp_path)) == 2

    def test_missing_dir_is_empty(self, tmp_path):
        assert load_heuristics(tmp_path) == []

    def test_malformed_file_is_empty(self, tmp_path):
        d = tmp_path / "heuristics"
        d.mkdir(parents=True)
        (d / "2026-07-28.json").write_text("not json", encoding="utf-8")
        assert load_heuristics(tmp_path) == []

    def test_entries_without_text_dropped(self, tmp_path):
        _store(tmp_path, [{"task_type": "coding", "pattern": ["a", "b"]}])
        assert load_heuristics(tmp_path) == []


class TestEvidenceRanking:
    def test_stronger_outcome_ranks_higher(self):
        weak = _h(["a", "b"], outcome=0.3)
        strong = _h(["c", "d"], outcome=1.0)
        ranked = retrieve("task", [weak, strong], task_type="coding")
        assert ranked[0].heuristic["pattern"] == ["c", "d"]

    def test_more_evidence_breaks_a_tie(self):
        thin = _h(["a", "b"], outcome=1.0, frequency=2)
        thick = _h(["c", "d"], outcome=1.0, frequency=8)
        assert retrieve("t", [thin, thick])[0].heuristic["pattern"] == ["c", "d"]

    def test_matching_task_type_outranks_stronger_evidence(self):
        """Relevance gates usefulness — a coding heuristic is near-useless on a
        research task however strong it is."""
        offtype = _h(["a", "b"], task_type="coding", outcome=1.0, frequency=8)
        ontype = _h(["c", "d"], task_type="research", outcome=0.4, frequency=2)
        ranked = retrieve("t", [offtype, ontype], task_type="research")
        assert ranked[0].heuristic["task_type"] == "research"

    def test_evidence_is_the_default_source(self):
        assert retrieve("t", [_h(["a", "b"])])[0].ranked_by == "evidence"

    def test_top_k_respected(self):
        hs = [_h([f"a{i}", f"b{i}"]) for i in range(10)]
        assert len(retrieve("t", hs, top_k=3)) == 3

    def test_zero_k_returns_nothing(self):
        assert retrieve("t", [_h(["a", "b"])], top_k=0) == []

    def test_empty_store(self):
        assert retrieve("t", []) == []

    def test_order_is_stable(self):
        """Ties broken deterministically, not by dict iteration order."""
        hs = [_h(["a", "b"]), _h(["c", "d"]), _h(["e", "f"])]
        assert [r.text for r in retrieve("t", hs)] == [r.text for r in retrieve("t", hs)]


class TestJudge:
    def test_judge_scores_are_used(self):
        hs = [_h(["a", "b"], outcome=1.0), _h(["c", "d"], outcome=0.1)]

        def judge(_ctx, h):
            return 1.0 if h["pattern"] == ["c", "d"] else 0.0

        ranked = retrieve("t", hs, judge=judge)
        assert ranked[0].heuristic["pattern"] == ["c", "d"]
        assert ranked[0].ranked_by == "judge"

    def test_judge_receives_context_and_heuristic(self):
        seen = []

        def judge(ctx, h):
            seen.append((ctx, h["pattern"]))
            return 0.5

        retrieve("fix the parser", [_h(["a", "b"])], judge=judge)
        assert seen == [("fix the parser", ["a", "b"])]

    def test_judge_scores_are_clamped(self):
        ranked = retrieve("t", [_h(["a", "b"])], judge=lambda c, h: 42.0)
        assert ranked[0].score == 1.0

    def test_failing_judge_degrades_to_evidence_not_empty(self):
        """An injection path that silently returns nothing is indistinguishable
        from one that had nothing to say."""
        def judge(_c, _h):
            raise RuntimeError("model unavailable")

        ranked = retrieve("t", [_h(["a", "b"])], judge=judge)
        assert len(ranked) == 1
        assert ranked[0].ranked_by == "evidence"

    def test_judge_returning_junk_degrades(self):
        ranked = retrieve("t", [_h(["a", "b"])], judge=lambda c, h: "not a number")
        assert ranked[0].ranked_by == "evidence"


class TestDedup:
    def test_same_claim_kept_once(self):
        """The same transition extracted on two days differs in prose but says
        one thing — injecting both spends prompt budget twice."""
        a = _h(["read_file", "patch"], frequency=3, text="v1 wording")
        b = _h(["read_file", "patch"], frequency=9, text="v2 wording")
        assert len(retrieve("t", [a, b])) == 1

    def test_first_seen_wins(self):
        a = _h(["a", "b"], text="first")
        b = _h(["a", "b"], text="second")
        assert retrieve("t", [a, b])[0].text == "first"

    def test_different_task_types_are_distinct(self):
        a = _h(["a", "b"], task_type="coding")
        b = _h(["a", "b"], task_type="research")
        assert len(retrieve("t", [a, b])) == 2


class TestFormatForInjection:
    def test_empty_renders_nothing(self):
        """So the caller can concatenate without emitting a bare header."""
        assert format_for_injection([]) == ""

    def test_renders_each_heuristic(self):
        ranked = retrieve("t", [_h(["read_file", "patch"]), _h(["patch", "terminal"])])
        out = format_for_injection(ranked)
        assert "Learned from past runs" in out
        assert out.count("- ") == 2


class TestCli:
    def test_exit_one_when_nothing_stored(self, tmp_path, capsys):
        assert main(["some task", "--evolution-dir", str(tmp_path)]) == 1
        capsys.readouterr()

    def test_selects_and_prints(self, tmp_path, capsys):
        _store(tmp_path, [_h(["read_file", "patch"]), _h(["a", "b"], task_type="research")])
        assert main(["fix it", "--evolution-dir", str(tmp_path), "--task-type", "coding"]) == 0
        out = json.loads(capsys.readouterr().out)
        assert out["considered"] == 2
        assert out["selected"][0]["heuristic"]["task_type"] == "coding"

    def test_top_k_flag(self, tmp_path, capsys):
        _store(tmp_path, [_h([f"a{i}", f"b{i}"]) for i in range(5)])
        assert main(["t", "--evolution-dir", str(tmp_path), "--top-k", "2"]) == 0
        assert len(json.loads(capsys.readouterr().out)["selected"]) == 2

    def test_bad_top_k_exits_two(self, tmp_path, capsys):
        assert main(["t", "--evolution-dir", str(tmp_path), "--top-k", "abc"]) == 2
        capsys.readouterr()

    def test_missing_flag_value_exits_two(self, capsys):
        assert main(["t", "--task-type"]) == 2
        capsys.readouterr()

    def test_help_exits_zero(self, capsys):
        assert main(["--help"]) == 0
        assert "usage" in capsys.readouterr().out
