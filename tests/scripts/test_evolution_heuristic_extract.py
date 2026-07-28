# -*- coding: utf-8 -*-
"""Tests for cross-task heuristic extraction (issue #1359, Child A of #1303)."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from evolution_heuristic_extract import (  # noqa: E402
    extract_heuristics,
    format_summary,
    load_trajectories,
    main,
    write_heuristics,
)


def _write(directory, session, turns):
    """turns: list of (completed, [tool, ...]); completed=None omits the field."""
    directory.mkdir(parents=True, exist_ok=True)
    p = directory / f"2026-07-28_{session}.jsonl"
    with open(p, "a", encoding="utf-8") as fh:
        for completed, tools in turns:
            rec = {"date": "2026-07-28", "session_id": session,
                   "entries": [{"tool": t, "result_status": "success"} for t in tools]}
            if completed is not None:
                rec["completed"] = completed
            fh.write(json.dumps(rec) + "\n")
    return p


class TestLoadTrajectories:
    def test_reads_recorded_outcomes(self, tmp_path):
        _write(tmp_path, "s1", [(True, ["read_file", "patch"]), (False, ["terminal"])])
        assert len(load_trajectories(tmp_path)) == 2

    def test_unrecorded_outcome_skipped(self, tmp_path):
        """A heuristic is a claim about what leads to success — a run whose
        outcome nobody wrote down cannot contribute to either side."""
        _write(tmp_path, "s1", [(None, ["read_file", "patch"])])
        assert load_trajectories(tmp_path) == []

    def test_ids_are_unique_per_turn(self, tmp_path):
        _write(tmp_path, "s1", [(True, ["a", "b"]), (True, ["c", "d"])])
        ids = [r["_id"] for r in load_trajectories(tmp_path)]
        assert len(set(ids)) == 2

    def test_malformed_lines_skipped(self, tmp_path):
        p = _write(tmp_path, "s1", [(True, ["read_file", "patch"])])
        with open(p, "a", encoding="utf-8") as fh:
            fh.write("not json\n")
            fh.write(json.dumps({"completed": True}) + "\n")
        assert len(load_trajectories(tmp_path)) == 1

    def test_missing_dir_is_empty(self, tmp_path):
        assert load_trajectories(tmp_path / "absent") == []


class TestExtractHeuristics:
    def test_empty_input(self):
        assert extract_heuristics([]) == []

    def test_single_trajectory_below_frequency(self, tmp_path):
        """One run is an anecdote, not a habit."""
        _write(tmp_path, "s1", [(True, ["read_file", "patch"])])
        assert extract_heuristics(load_trajectories(tmp_path)) == []

    def test_recurring_success_pattern_extracted(self, tmp_path):
        _write(tmp_path, "s1", [(True, ["read_file", "patch"])] * 3)
        hs = extract_heuristics(load_trajectories(tmp_path))
        assert hs
        assert hs[0].pattern == ["read_file", "patch"]
        assert hs[0].outcome_score == 1.0
        assert hs[0].success_count == 3

    def test_pattern_in_both_outcomes_scores_zero_and_is_dropped(self, tmp_path):
        """Equally common in success and failure = says nothing about outcome."""
        _write(tmp_path, "s1", [(True, ["a", "b"]), (False, ["a", "b"])])
        assert extract_heuristics(load_trajectories(tmp_path)) == []

    def test_failure_associated_pattern_not_returned(self, tmp_path):
        """These are injected into a prompt as advice, so only positives."""
        _write(tmp_path, "s1", [(False, ["a", "b"])] * 3)
        assert extract_heuristics(load_trajectories(tmp_path)) == []

    def test_counted_once_per_trajectory(self, tmp_path):
        """A loop repeating one transition ten times is one habit, not ten
        pieces of evidence."""
        _write(tmp_path, "s1", [(True, ["patch", "terminal"] * 5)] * 2)
        hs = extract_heuristics(load_trajectories(tmp_path))
        assert hs and all(h.frequency == 2 for h in hs)

    def test_source_trajectories_recorded(self, tmp_path):
        _write(tmp_path, "s1", [(True, ["read_file", "patch"])] * 2)
        h = extract_heuristics(load_trajectories(tmp_path))[0]
        assert len(h.source_trajectories) == 2
        assert all("#" in s for s in h.source_trajectories)

    def test_task_types_kept_separate(self, tmp_path):
        _write(tmp_path, "s1", [(True, ["read_file", "search_files"])] * 2)
        _write(tmp_path, "s2", [(True, ["patch", "execute_code"])] * 2)
        types = {h.task_type for h in extract_heuristics(load_trajectories(tmp_path))}
        assert len(types) >= 2

    def test_strongest_signal_first(self, tmp_path):
        _write(tmp_path, "s1", [(True, ["a", "b"])] * 4)
        _write(tmp_path, "s2", [(True, ["c", "d"]), (True, ["c", "d"]), (False, ["c", "d"])])
        hs = extract_heuristics(load_trajectories(tmp_path))
        assert hs[0].outcome_score >= hs[-1].outcome_score

    def test_thresholds_respected(self, tmp_path):
        _write(tmp_path, "s1", [(True, ["a", "b"])] * 2)
        assert extract_heuristics(load_trajectories(tmp_path), min_frequency=3) == []
        assert extract_heuristics(load_trajectories(tmp_path), min_frequency=2)

    def test_single_call_turn_has_no_transition(self, tmp_path):
        _write(tmp_path, "s1", [(True, ["read_file"])] * 3)
        assert extract_heuristics(load_trajectories(tmp_path)) == []

    def test_heuristic_text_states_the_evidence(self, tmp_path):
        """Falsifiable: the claim carries the counts it rests on."""
        _write(tmp_path, "s1", [(True, ["read_file", "patch"])] * 3)
        text = extract_heuristics(load_trajectories(tmp_path))[0].text
        assert "read_file" in text and "patch" in text
        assert "3 successful" in text


class TestWriteAndFormat:
    def test_writes_dated_file(self, tmp_path):
        _write(tmp_path / "trajectories", "s1", [(True, ["read_file", "patch"])] * 2)
        hs = extract_heuristics(load_trajectories(tmp_path / "trajectories"))
        path = write_heuristics(hs, tmp_path, date="2026-07-28")
        assert path.name == "2026-07-28.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["count"] == len(hs)
        assert data["heuristics"][0]["pattern"] == ["read_file", "patch"]

    def test_writes_empty_set_too(self, tmp_path):
        """An empty result is a finding — 'we looked and found nothing'."""
        path = write_heuristics([], tmp_path, date="2026-07-28")
        assert json.loads(path.read_text(encoding="utf-8"))["count"] == 0

    def test_summary_when_empty(self):
        assert "none extracted" in format_summary([])

    def test_summary_names_the_strongest(self, tmp_path):
        _write(tmp_path, "s1", [(True, ["read_file", "patch"])] * 3)
        out = format_summary(extract_heuristics(load_trajectories(tmp_path)))
        assert "read_file->patch" in out


class TestCli:
    def test_exit_one_when_nothing_extracted(self, tmp_path, capsys):
        assert main(["--evolution-dir", str(tmp_path)]) == 1
        capsys.readouterr()

    def test_exit_zero_and_writes(self, tmp_path, capsys):
        _write(tmp_path / "trajectories", "s1", [(True, ["read_file", "patch"])] * 3)
        assert main(["--evolution-dir", str(tmp_path)]) == 0
        out = json.loads(capsys.readouterr().out)
        assert out["trajectories_read"] == 3
        assert Path(out["written_to"]).exists()

    def test_thresholds_from_cli(self, tmp_path, capsys):
        _write(tmp_path / "trajectories", "s1", [(True, ["a", "b"])] * 2)
        assert main(["--evolution-dir", str(tmp_path), "--min-frequency", "5"]) == 1
        capsys.readouterr()

    def test_bad_threshold_exits_two(self, tmp_path, capsys):
        assert main(["--evolution-dir", str(tmp_path), "--min-frequency", "abc"]) == 2
        capsys.readouterr()

    def test_missing_flag_value_exits_two(self, capsys):
        assert main(["--min-score"]) == 2
        capsys.readouterr()

    def test_help_exits_zero(self, capsys):
        assert main(["--help"]) == 0
        assert "usage" in capsys.readouterr().out
