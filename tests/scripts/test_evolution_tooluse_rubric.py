# -*- coding: utf-8 -*-
"""Tests for the tool-use competency rubric (issue #1268)."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from evolution_tooluse_rubric import (  # noqa: E402
    DIMENSIONS,
    format_summary,
    load_turns,
    main,
    score_corpus,
    score_turn,
)


def _entry(tool, args=None, status="success", summary=""):
    return {
        "tool": tool,
        "args_summary": args or {},
        "result_status": status,
        "result_summary": summary,
    }


def _write_turns(d, session, turns):
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"2026-07-28_{session}.jsonl"
    with open(p, "a", encoding="utf-8") as fh:
        for entries in turns:
            fh.write(json.dumps({"date": "2026-07-28", "session_id": session,
                                 "entries": entries}) + "\n")
    return p


class TestCleanTurn:
    def test_varied_successful_calls_score_well(self):
        s = score_turn([_entry("read_file", {"p": "a"}), _entry("patch", {"p": "a"}),
                        _entry("terminal", {"c": "test"})])
        assert s.overall == 1.0
        assert all(s.scores[d] == 1.0 for d in DIMENSIONS)

    def test_empty_turn_is_not_a_free_pass(self):
        """Nothing was attempted, so there is nothing to be competent at —
        averaging in a 1.0 would flatter the corpus figure."""
        s = score_turn([])
        assert s.n_calls == 0
        assert s.scores == {}
        assert s.overall == 0.0


class TestErrorRecovery:
    def test_identical_retry_after_failure_is_penalised(self):
        """The infinite-debugging-loop shape LHTB names."""
        s = score_turn([_entry("terminal", {"c": "x"}, "failure"),
                        _entry("terminal", {"c": "x"}, "failure")])
        assert s.scores["error_recovery"] < 1.0
        assert any("identical retry" in n for n in s.notes)

    def test_changing_approach_after_failure_is_not_penalised(self):
        s = score_turn([_entry("terminal", {"c": "x"}, "failure"),
                        _entry("read_file", {"p": "a"})])
        assert s.scores["error_recovery"] == 1.0

    def test_no_failures_scores_full(self):
        s = score_turn([_entry("read_file", {"p": "a"}), _entry("patch", {"p": "b"})])
        assert s.scores["error_recovery"] == 1.0

    def test_two_identical_retries_score_zero(self):
        s = score_turn([_entry("terminal", {"c": "x"}, "failure")] * 3)
        assert s.scores["error_recovery"] == 0.0


class TestParameterization:
    def test_argument_correction_after_failure_is_counted(self):
        s = score_turn([_entry("read_file", {"p": "wrong"}, "failure"),
                        _entry("read_file", {"p": "right"})])
        assert s.scores["parameterization"] < 1.0
        assert any("argument correction" in n for n in s.notes)

    def test_first_time_right_scores_full(self):
        s = score_turn([_entry("read_file", {"p": "right"})])
        assert s.scores["parameterization"] == 1.0

    def test_a_different_tool_after_failure_is_not_a_correction(self):
        s = score_turn([_entry("read_file", {"p": "x"}, "failure"), _entry("patch", {"p": "y"})])
        assert s.scores["parameterization"] == 1.0


class TestDiscovery:
    def test_a_couple_of_searches_is_within_tolerance(self):
        s = score_turn([_entry("tool_search", {"q": "a"}), _entry("tool_search", {"q": "b"}),
                        _entry("read_file", {"p": "x"})])
        assert s.scores["discovery"] == 1.0

    def test_reformulating_repeatedly_is_penalised(self):
        entries = [_entry("tool_search", {"q": f"query {i}"}) for i in range(6)]
        entries.append(_entry("read_file", {"p": "x"}))
        s = score_turn(entries)
        assert s.scores["discovery"] < 1.0
        assert any("discovery calls" in n for n in s.notes)

    def test_no_search_at_all_scores_full(self):
        assert score_turn([_entry("read_file", {"p": "x"})]).scores["discovery"] == 1.0


class TestSyntaxAndEfficiency:
    def test_parse_error_lowers_syntax(self):
        s = score_turn([_entry("terminal", {"c": "x"}, "failure", "invalid json in response")])
        assert s.scores["syntax"] < 1.0

    def test_redundant_calls_lower_efficiency(self):
        s = score_turn([_entry("read_file", {"p": "a"})] * 4)
        assert s.scores["efficiency"] == 0.25
        assert any("redundant" in n for n in s.notes)

    def test_all_distinct_scores_full_efficiency(self):
        s = score_turn([_entry("read_file", {"p": f"f{i}"}) for i in range(4)])
        assert s.scores["efficiency"] == 1.0


class TestLoadTurnsPreservesBoundaries:
    """The previous attempt flattened independent sessions and assigned every
    call turn 0, manufacturing false repeated-call clusters (PR #1281)."""

    def test_turn_index_is_per_file(self, tmp_path):
        _write_turns(tmp_path, "s1", [[_entry("a")], [_entry("b")], [_entry("c")]])
        turns = load_turns(tmp_path)
        assert [t["_turn_index"] for t in turns] == [0, 1, 2]

    def test_sessions_stay_separate(self, tmp_path):
        _write_turns(tmp_path, "s1", [[_entry("a")]])
        _write_turns(tmp_path, "s2", [[_entry("b")]])
        assert {t["session_id"] for t in load_turns(tmp_path)} == {"s1", "s2"}

    def test_funnel_self_record_is_ignored(self, tmp_path):
        """Scoring the pipeline's record of its own invocation is what got the
        last attempt closed as incoherent."""
        tmp_path.mkdir(parents=True, exist_ok=True)
        (tmp_path / "2026-07-28.json").write_text(json.dumps({
            "date": "2026-07-28", "session_id": "",
            "entries": [_entry("evolution_funnel")],
        }), encoding="utf-8")
        assert load_turns(tmp_path) == []

    def test_malformed_lines_skipped(self, tmp_path):
        p = _write_turns(tmp_path, "s1", [[_entry("a")]])
        with open(p, "a", encoding="utf-8") as fh:
            fh.write("not json\n")
            fh.write(json.dumps({"no": "entries"}) + "\n")
        assert len(load_turns(tmp_path)) == 1

    def test_missing_dir_is_empty(self, tmp_path):
        assert load_turns(tmp_path / "absent") == []


class TestScoreCorpus:
    def test_turns_scored_separately_not_concatenated(self, tmp_path):
        """Two turns each calling read_file once are NOT a redundant pair —
        concatenating them would say otherwise."""
        _write_turns(tmp_path, "s1", [[_entry("read_file", {"p": "a"})],
                                      [_entry("read_file", {"p": "a"})]])
        summary = score_corpus(load_turns(tmp_path))
        assert summary["dimensions"]["efficiency"] == 1.0
        assert summary["turns_scored"] == 2

    def test_empty_turns_excluded_from_averages(self, tmp_path):
        _write_turns(tmp_path, "s1", [[], [_entry("read_file", {"p": "a"})]])
        summary = score_corpus(load_turns(tmp_path))
        assert summary["turns_seen"] == 2
        assert summary["turns_scored"] == 1

    def test_counts_sessions_and_calls(self, tmp_path):
        _write_turns(tmp_path, "s1", [[_entry("a"), _entry("b")]])
        _write_turns(tmp_path, "s2", [[_entry("c")]])
        summary = score_corpus(load_turns(tmp_path))
        assert summary["sessions"] == 2
        assert summary["total_calls"] == 3

    def test_empty_corpus_reports_none_not_zero(self):
        """No data is not a score of zero — a consumer must be able to tell
        'not measured' from 'measured badly'."""
        summary = score_corpus([])
        assert summary["overall"] is None
        assert all(v is None for v in summary["dimensions"].values())


class TestCli:
    def test_summary_json_and_exit_zero(self, tmp_path, capsys):
        _write_turns(tmp_path, "s1", [[_entry("read_file", {"p": "a"})]])
        assert main(["--trajectory-dir", str(tmp_path)]) == 0
        out = json.loads(capsys.readouterr().out)
        assert out["turns_scored"] == 1

    def test_missing_dir_argument_exits_two(self, capsys):
        assert main(["--trajectory-dir"]) == 2
        capsys.readouterr()

    def test_help_exits_zero(self, capsys):
        assert main(["--help"]) == 0
        assert "usage" in capsys.readouterr().out

    def test_format_summary_handles_empty(self):
        assert "no captured turns" in format_summary(score_corpus([]))
