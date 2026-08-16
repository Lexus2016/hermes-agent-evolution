"""Tests for the long-horizon stress bucket (#2530, LongHorizon-Harness).

Behavior contracts, not snapshots:
  * task defs parse, carry the horizon marker (min_turns >= 50), and stay out
    of the default split;
  * the runner routes flagged tasks into the stress bucket and floors the
    iteration cap at the task's own horizon;
  * a run that finishes a 50+ turn chain too fast is marked ``shortcut`` and
    scores zero, while a run that walks the chain is not penalized;
  * the bucket is opt-in (``--with-long-horizon``), reported as its own
    section, and absent from the default run.

Pure stdlib + pytest; the agent and conversation driver are stubbed at the
seams ``run_eval_task`` exposes — no network, no LLM calls.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from eval_baseline import main as baseline_main  # noqa: E402
from eval_baseline import run_long_horizon_stress, run_null_agent_baseline  # noqa: E402
from eval_runner import (  # noqa: E402
    DEFAULT_MAX_ITERATIONS,
    LONG_HORIZON_BUCKET,
    horizon_bucket,
    run_eval_task,
    split_by_horizon,
)
from eval_tasks import (  # noqa: E402
    LONG_HORIZON_MIN_TURNS,
    EvalTask,
    load_long_horizon_tasks,
    load_task_set,
)


def _task(**kw) -> EvalTask:
    base = {
        "id": "t1",
        "prompt": "do the thing",
        "expected_tools": ["search_files"],
        "expected_result_pattern": "found-it",
        "category": "search",
        "difficulty": "easy",
    }
    base.update(kw)
    return EvalTask(**base)


def _fake_agent_factory(_pinned, on_start, on_complete):
    return SimpleNamespace(on_start=on_start, on_complete=on_complete)


def _driver(turns=1, answer="found-it"):
    """A conversation driver that reports ``turns`` assistant messages."""

    def drive(_agent, _prompt, **_kw):
        return {
            "response": answer,
            "conversation_history": [
                {"role": "assistant", "content": answer} for _ in range(turns)
            ],
            "failed": False,
        }

    return drive


class TestTaskDefs:
    def test_defs_parse_and_carry_the_horizon_marker(self):
        tasks = load_long_horizon_tasks()
        assert 2 <= len(tasks) <= 3
        assert all(isinstance(t, EvalTask) for t in tasks)
        for t in tasks:
            assert t.min_turns >= LONG_HORIZON_MIN_TURNS  # the 50+ marker
            assert t.id.startswith("lh-")
            assert t.prompt and t.expected_tools

    def test_loader_returns_a_fresh_copy(self):
        assert load_long_horizon_tasks() is not load_long_horizon_tasks()

    def test_ids_unique_and_disjoint_from_default_split(self):
        stress_ids = {t.id for t in load_long_horizon_tasks()}
        default_ids = {t.id for t in load_task_set()}
        assert len(stress_ids) == len(load_long_horizon_tasks())
        assert stress_ids & default_ids == set()

    def test_default_split_carries_no_horizon_marker(self):
        assert all(t.min_turns == 0 for t in load_task_set())


class TestRunnerBucketing:
    def test_horizon_bucket_classifies_by_min_turns(self):
        assert horizon_bucket(_task(min_turns=50)) == LONG_HORIZON_BUCKET
        assert horizon_bucket(_task()) is None
        assert horizon_bucket(_task(min_turns=0)) is None

    def test_split_by_horizon_partitions_all_tasks(self):
        mixed = load_task_set() + load_long_horizon_tasks()
        default, stress = split_by_horizon(mixed)
        assert default == load_task_set()
        assert stress == load_long_horizon_tasks()
        assert len(default) + len(stress) == len(mixed)

    def test_iteration_cap_floored_at_task_horizon(self):
        traj = run_eval_task(
            _task(min_turns=50), None, _fake_agent_factory, _driver(turns=50)
        )
        assert traj.agent_config["max_iterations"] >= 50

    def test_default_tasks_keep_the_pinned_cap(self):
        traj = run_eval_task(
            _task(), None, _fake_agent_factory, _driver(turns=1)
        )
        assert traj.agent_config["max_iterations"] == DEFAULT_MAX_ITERATIONS


class TestShortcutRule:
    def test_finished_too_fast_is_marked_shortcut(self):
        traj = run_eval_task(
            _task(min_turns=50), None, _fake_agent_factory, _driver(turns=3)
        )
        assert traj.error is not None
        assert traj.error.startswith("shortcut:")

    def test_shortcut_scores_zero_through_the_error_gate(self):
        from eval_baseline import score_trajectory

        traj = run_eval_task(
            _task(min_turns=50), None, _fake_agent_factory, _driver(turns=3)
        )
        assert score_trajectory(traj, _task(min_turns=50))["total"] == 0.0

    def test_a_run_that_walks_the_chain_is_not_penalized(self):
        # exactly at the horizon boundary is still an honest completion
        traj = run_eval_task(
            _task(min_turns=50), None, _fake_agent_factory, _driver(turns=50)
        )
        assert traj.error is None
        assert traj.model_turns == 50

    def test_default_task_1_turn_completion_stays_clean(self):
        """Regression guard: the shortcut rule must not leak to normal tasks."""
        traj = run_eval_task(_task(), None, _fake_agent_factory, _driver(turns=1))
        assert traj.error is None


def _parse_reports(out: str):
    """Parse consecutive (pretty-printed) JSON objects printed to stdout."""
    decoder = json.JSONDecoder()
    reports, idx = [], 0
    while idx < len(out):
        if out[idx] == "{":
            obj, end = decoder.raw_decode(out, idx)
            reports.append(obj)
            idx = end
        else:
            idx += 1
    return reports


class TestBaselineIntegration:
    def test_null_agent_floor_is_zero_on_the_stress_bucket(self, tmp_path, capsys):
        scores, summary = run_long_horizon_stress(
            agent="null", scores_path=str(tmp_path / "s-lh.jsonl")
        )
        assert summary["tasks"] == len(load_long_horizon_tasks())
        assert summary["mean_total"] == 0.0
        # the null agent refuses in 1 turn -> every task is a shortcut
        assert scores and all(s["error"] and s["error"].startswith("shortcut:") for s in scores)
        assert (tmp_path / "s-lh.jsonl").exists()

    def test_default_run_excludes_the_stress_bucket(self, tmp_path):
        _, summary = run_null_agent_baseline(scores_path=str(tmp_path / "s.jsonl"))
        assert summary["tasks"] == len(load_task_set())

    def test_cli_flag_adds_the_stress_section(self, tmp_path, capsys):
        rc = baseline_main(
            ["-o", str(tmp_path / "s.jsonl"), "--with-long-horizon"]
        )
        assert rc == 0
        reports = _parse_reports(capsys.readouterr().out)
        # default split section (no bucket key) + separate stress section
        assert any(
            r.get("bucket") is None and r.get("tasks") == len(load_task_set())
            for r in reports
        )
        assert any(r.get("bucket") == LONG_HORIZON_BUCKET for r in reports)
        assert (tmp_path / "s-long-horizon.jsonl").exists()

    def test_cli_without_flag_has_no_stress_section(self, tmp_path, capsys):
        rc = baseline_main(["-o", str(tmp_path / "s.jsonl")])
        assert rc == 0
        out = capsys.readouterr().out
        assert "long-horizon-stress" not in out
        assert not (tmp_path / "s-long-horizon.jsonl").exists()
