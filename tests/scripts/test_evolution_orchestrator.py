"""Tests for scripts/evolution_orchestrator.py — deterministic fan-out +
collection halves of the orchestrator-workers research loop (#300)."""

import io
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from evolution_orchestrator import (  # noqa: E402
    DEFAULT_MAX_WORKERS,
    DEFAULT_WORKER_TOOLSETS,
    build_worker_task,
    build_worker_tasks,
    collect_candidates,
    main,
)


class TestBuildWorkerTask:
    def test_single_task_is_self_contained(self):
        t = build_worker_task("the sub-task", "official docs")
        # The angle and the sub-task both appear in the goal (workers have no
        # shared memory, so each prompt must stand alone).
        assert "the sub-task" in t["goal"]
        assert "official docs" in t["goal"]
        assert "the sub-task" in t["context"]
        assert t["role"] == "leaf"
        assert t["toolsets"] == list(DEFAULT_WORKER_TOOLSETS)

    def test_custom_toolsets_respected(self):
        t = build_worker_task("s", "a", toolsets=["web"])
        assert t["toolsets"] == ["web"]

    def test_whitespace_stripped(self):
        t = build_worker_task("  s  ", "  a  ")
        assert "SUB-TASK: s" in t["goal"]
        assert "YOUR ANGLE: a" in t["goal"]


class TestBuildWorkerTasks:
    def test_one_task_per_angle_order_preserved(self):
        tasks, dropped = build_worker_tasks("st", ["a", "b", "c"], max_workers=3)
        assert dropped == 0
        assert len(tasks) == 3
        # Order preserved so a candidate's task_index maps back to its angle.
        assert "YOUR ANGLE: a" in tasks[0]["goal"]
        assert "YOUR ANGLE: b" in tasks[1]["goal"]
        assert "YOUR ANGLE: c" in tasks[2]["goal"]

    def test_caps_at_max_workers_and_reports_dropped(self):
        tasks, dropped = build_worker_tasks("st", ["a", "b", "c", "d", "e"], max_workers=3)
        assert len(tasks) == 3
        assert dropped == 2
        # The KEPT tasks are the first three, in order.
        assert "YOUR ANGLE: a" in tasks[0]["goal"]
        assert "YOUR ANGLE: c" in tasks[2]["goal"]

    def test_default_max_workers(self):
        tasks, dropped = build_worker_tasks("st", ["a", "b", "c", "d"])
        assert len(tasks) == DEFAULT_MAX_WORKERS
        assert dropped == 4 - DEFAULT_MAX_WORKERS

    def test_blank_angles_skipped_not_dropped(self):
        # Blank angles are removed before the cap and do NOT count as dropped.
        tasks, dropped = build_worker_tasks("st", ["a", "  ", "", "b"], max_workers=3)
        assert len(tasks) == 2
        assert dropped == 0

    def test_max_workers_floor_is_one(self):
        tasks, dropped = build_worker_tasks("st", ["a", "b"], max_workers=0)
        assert len(tasks) == 1
        assert dropped == 1


class TestCollectCandidates:
    def _delegate_output(self):
        # The exact shape delegate_task returns.
        return {
            "results": [
                {"task_index": 0, "status": "completed", "summary": "finding A"},
                {"task_index": 1, "status": "completed", "summary": "finding B"},
            ],
            "total_duration_seconds": 1.2,
        }

    def test_maps_results_to_candidates(self):
        cands, ok, failed = collect_candidates(self._delegate_output(), ["ang0", "ang1"])
        assert ok == 2
        assert failed == 0
        assert [c["candidate"] for c in cands] == ["finding A", "finding B"]
        assert [c["angle"] for c in cands] == ["ang0", "ang1"]
        # scores left EMPTY — scoring is the evaluator's job.
        assert all(c["scores"] == {} for c in cands)
        assert all(c["ok"] for c in cands)

    def test_accepts_bare_results_list(self):
        out = self._delegate_output()["results"]
        cands, ok, failed = collect_candidates(out)
        assert ok == 2
        assert [c["angle"] for c in cands] == [None, None]

    def test_failed_worker_is_candidate_but_not_ok(self):
        out = {
            "results": [
                {"task_index": 0, "status": "completed", "summary": "good"},
                {"task_index": 1, "status": "timeout", "summary": ""},
                {"task_index": 2, "status": "error", "summary": "partial"},
            ]
        }
        cands, ok, failed = collect_candidates(out)
        assert ok == 1
        assert failed == 2
        # Every attempt yields a candidate (honest count), but failures are ok=False.
        assert len(cands) == 3
        by_index = {c["index"]: c for c in cands}
        assert by_index[0]["ok"] is True
        assert by_index[1]["ok"] is False  # empty summary
        assert by_index[2]["ok"] is False  # error status, even with text

    def test_completed_but_empty_summary_is_not_ok(self):
        out = {"results": [{"task_index": 0, "status": "completed", "summary": "   "}]}
        cands, ok, failed = collect_candidates(out)
        assert ok == 0
        assert failed == 1
        assert cands[0]["ok"] is False

    def test_candidates_sorted_by_index(self):
        out = {
            "results": [
                {"task_index": 2, "status": "completed", "summary": "c"},
                {"task_index": 0, "status": "completed", "summary": "a"},
                {"task_index": 1, "status": "completed", "summary": "b"},
            ]
        }
        cands, _, _ = collect_candidates(out, ["a0", "a1", "a2"])
        assert [c["index"] for c in cands] == [0, 1, 2]
        assert [c["candidate"] for c in cands] == ["a", "b", "c"]
        assert [c["angle"] for c in cands] == ["a0", "a1", "a2"]

    def test_garbage_input_does_not_crash(self):
        for bad in (None, 42, "string", {"no_results": True}, {"results": "nope"}):
            cands, ok, failed = collect_candidates(bad)
            assert cands == []
            assert ok == 0
            assert failed == 0

    def test_missing_task_index_falls_back_to_position(self):
        out = {"results": [{"status": "completed", "summary": "x"}]}
        cands, ok, _ = collect_candidates(out)
        assert ok == 1
        assert cands[0]["index"] == 0

    def test_output_feeds_evaluator_shape(self):
        # The collected payload must be consumable by evolution_evaluator.
        sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
        from evolution_evaluator import decide  # noqa: E402

        cands, _, _ = collect_candidates(self._delegate_output())
        # Empty scores -> evaluator can't judge -> nobody passes (correct: the
        # orchestrator collects, the evaluator scores; this skill never fakes a pass).
        payload = {"candidates": cands}
        result = decide(payload["candidates"], threshold=0.75, current_pass=1, max_passes=3)
        assert result["best_score"] == 0.0
        # With budget left, an all-unscored set is OPTIMIZE, never a crash.
        assert result["verdict"] == "OPTIMIZE"


class TestCLI:
    def test_build_emits_tasks(self, capsys):
        rc = main(
            [
                "evolution_orchestrator.py",
                "build",
                "--subtask",
                "how do agents bound depth",
                "--angle",
                "docs",
                "--angle",
                "failures",
            ]
        )
        assert rc == 0
        out = json.loads(capsys.readouterr().out)
        assert len(out["tasks"]) == 2
        assert out["dropped"] == 0

    def test_build_requires_subtask(self, capsys):
        rc = main(["evolution_orchestrator.py", "build", "--angle", "x"])
        assert rc == 2

    def test_build_requires_angle(self, capsys):
        rc = main(["evolution_orchestrator.py", "build", "--subtask", "s"])
        assert rc == 2

    def test_build_caps_and_reports_dropped(self, capsys):
        rc = main(
            [
                "evolution_orchestrator.py",
                "build",
                "--subtask",
                "s",
                "--max-workers",
                "2",
                "--angle",
                "a",
                "--angle",
                "b",
                "--angle",
                "c",
            ]
        )
        assert rc == 0
        out = json.loads(capsys.readouterr().out)
        assert len(out["tasks"]) == 2
        assert out["dropped"] == 1

    def test_collect_from_stdin(self, capsys, monkeypatch):
        payload = json.dumps(
            {"results": [{"task_index": 0, "status": "completed", "summary": "x"}]}
        )
        monkeypatch.setattr("sys.stdin", io.StringIO(payload))
        rc = main(["evolution_orchestrator.py", "collect"])
        assert rc == 0
        out = json.loads(capsys.readouterr().out)
        assert out["ok"] == 1
        assert out["failed"] == 0
        assert out["candidates"][0]["candidate"] == "x"

    def test_collect_with_angles_file(self, capsys, tmp_path):
        angles = tmp_path / "angles.json"
        angles.write_text(json.dumps(["docs", "failures"]), encoding="utf-8")
        results = tmp_path / "results.json"
        results.write_text(
            json.dumps(
                {
                    "results": [
                        {"task_index": 0, "status": "completed", "summary": "A"},
                        {"task_index": 1, "status": "completed", "summary": "B"},
                    ]
                }
            ),
            encoding="utf-8",
        )
        rc = main(
            ["evolution_orchestrator.py", "collect", str(results), "--angles", str(angles)]
        )
        assert rc == 0
        out = json.loads(capsys.readouterr().out)
        assert [c["angle"] for c in out["candidates"]] == ["docs", "failures"]

    def test_collect_bad_json(self, capsys, monkeypatch):
        monkeypatch.setattr("sys.stdin", io.StringIO("{not json"))
        rc = main(["evolution_orchestrator.py", "collect"])
        assert rc == 2

    def test_no_command_is_usage_error(self, capsys):
        assert main(["evolution_orchestrator.py"]) == 2

    def test_unknown_command(self, capsys):
        assert main(["evolution_orchestrator.py", "frobnicate"]) == 2


class TestSkillFrontmatter:
    def test_skill_md_frontmatter_parses(self):
        # Validate the SKILL.md frontmatter parses via the same util the runtime uses.
        repo = Path(__file__).resolve().parents[2]
        sys.path.insert(0, str(repo))
        from agent.skill_utils import parse_frontmatter  # noqa: E402

        skill = repo / "skills" / "evolution" / "evolution-orchestrator" / "SKILL.md"
        fm, body = parse_frontmatter(skill.read_text(encoding="utf-8"))
        assert fm.get("name") == "evolution-orchestrator"
        assert fm.get("category") == "evolution"
        assert fm.get("version") == "1.0.0"
        assert "delegate_task" in body
