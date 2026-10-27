"""Recorded-input replay gate (#3517): a passing replay is a no-op, a replay
that misses the recorded grade exits 1 with one reason line."""

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from evolution_replay_gate import check_record, main
from evolution_trajectory_logger import TrajectoryLog

BASE = (
    "---\nname: example\ndescription: Example routing\ncontract:\n  version: 1\n"
    "  intent: Research\n  behavioral_criteria: [routing-applicability]\n"
    "  applies_to: [research]\n  excludes: [email]\n"
    "  protected_constraints: [Keep sources.]\n  replay_cases:\n"
    "    - {id: positive, task: research, applicable: true}\n---\nKeep sources.\n"
)


def _git(cwd, *args):
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


def _repo(tmp_path):
    _git(tmp_path, "init")
    _git(tmp_path, "config", "user.email", "t@example.com")
    _git(tmp_path, "config", "user.name", "Test")
    target = tmp_path / "skills/example/SKILL.md"
    target.parent.mkdir(parents=True)
    target.write_text(BASE, encoding="utf-8")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-m", "baseline")
    return target, _git(tmp_path, "rev-parse", "HEAD")


def _rec(tmp_path, payload, name="record.json"):
    path = tmp_path / name
    path.write_text(json.dumps(payload), encoding="utf-8")
    return str(path)


def _trace(tmp_path, *statuses, name="trace.json"):
    """One JSON object per status: a single-turn .json or a multi-turn .jsonl."""
    lines = []
    for i, status in enumerate(statuses):
        log = TrajectoryLog(session_id="s1", completed=True)
        log.add_tool_call(f"tool_{i}", {"a": i}, result="ok", status=status)
        lines.append(json.dumps(log.to_dict(), sort_keys=True))
    path = tmp_path / name
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(path)


def test_skill_replay(tmp_path, capsys):
    target, base = _repo(tmp_path)
    target.write_text(BASE.replace("version: 1", "version: 2") + "More.\n")
    rec = {"kind": "skill", "root": str(tmp_path), "base": base, "grade": "pass"}
    assert main(["g", _rec(tmp_path, rec)]) == 0
    target.write_text(BASE + "Changed without version bump.\n")
    assert main(["g", _rec(tmp_path, rec, "bad.json")]) == 1
    assert capsys.readouterr().err.startswith("replay gate: rejected:")


def test_trace_replay(tmp_path, capsys):
    ok = _rec(
        tmp_path,
        {"kind": "trace", "path": _trace(tmp_path, "success"), "grade": "pass"},
    )
    assert main(["g", ok]) == 0
    bad = _rec(
        tmp_path,
        {"kind": "trace", "path": _trace(tmp_path, "failure"), "grade": "pass"},
        "bad.json",
    )
    assert main(["g", bad]) == 1
    assert len(capsys.readouterr().err.strip().splitlines()) == 1


def test_multiturn_later_failure_rejected(tmp_path, capsys):
    """First turn success, second turn failure, recorded grade pass: the old
    first-turn-only grading accepted this. Every turn must be replayed."""
    path = _trace(tmp_path, "success", "failure", name="session.jsonl")
    rec = _rec(tmp_path, {"kind": "trace", "path": path, "grade": "pass"}, "multi.json")
    assert main(["g", rec]) == 1
    err = capsys.readouterr().err.strip()
    assert err.startswith("replay gate: rejected:") and "turn 2" in err
    assert len(err.splitlines()) == 1


def test_recorded_fail_grade_and_bad_input(tmp_path, capsys):
    trace = _trace(tmp_path, "failure")
    assert check_record({"kind": "trace", "path": trace, "grade": "fail"}) is None
    assert main(["g", _rec(tmp_path, {"kind": "nope", "grade": "pass"})]) == 2
    assert "bad record" in capsys.readouterr().err
    assert main(["g"]) == 2
