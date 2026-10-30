"""Recorded-input replay gate (#3517): a passing replay is a no-op, a replay
that misses the recorded grade exits 1 with one reason line, and the grades
keep the replay vocabulary — an unfinished or empty session is never reported
as a failure."""

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from evolution_replay_gate import (
    EXIT_BAD_INPUT,
    EXIT_NOT_REPLAYABLE,
    EXIT_OK,
    EXIT_REJECTED,
    NotReplayable,
    check_record,
    check_shipped_record,
    main,
    replay_grade,
)
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
    path.write_text(json.dumps(payload), encoding="utf-8-sig")
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


def _empty_trace(tmp_path, name="empty.jsonl"):
    """A session with no tool calls at all — the replay window is empty."""
    log = TrajectoryLog(session_id="s1", completed=True)
    path = tmp_path / name
    path.write_text(json.dumps(log.to_dict(), sort_keys=True) + "\n", encoding="utf-8")
    return str(path)


def test_skill_replay(tmp_path, capsys):
    target, base = _repo(tmp_path)
    target.write_text(BASE.replace("version: 1", "version: 2") + "More.\n")
    rec = {"kind": "skill", "root": str(tmp_path), "base": base, "grade": "pass"}
    assert main(["g", _rec(tmp_path, rec)]) == EXIT_OK
    target.write_text(BASE + "Changed without version bump.\n")
    assert main(["g", _rec(tmp_path, rec, "bad.json")]) == EXIT_REJECTED
    assert capsys.readouterr().err.startswith("replay gate: rejected:")


def test_trace_replay(tmp_path, capsys):
    ok = _rec(
        tmp_path,
        {"kind": "trace", "path": _trace(tmp_path, "success"), "grade": "pass"},
    )
    assert main(["g", ok]) == EXIT_OK
    bad = _rec(
        tmp_path,
        {"kind": "trace", "path": _trace(tmp_path, "failure"), "grade": "pass"},
        "bad.json",
    )
    assert main(["g", bad]) == EXIT_REJECTED
    assert len(capsys.readouterr().err.strip().splitlines()) == 1


def test_multiturn_later_failure_rejected(tmp_path, capsys):
    """First turn success, second turn failure, recorded grade pass: the old
    first-turn-only grading accepted this. Every turn must be replayed."""
    path = _trace(tmp_path, "success", "failure", name="session.jsonl")
    rec = _rec(tmp_path, {"kind": "trace", "path": path, "grade": "pass"}, "multi.json")
    assert main(["g", rec]) == EXIT_REJECTED
    err = capsys.readouterr().err.strip()
    assert err.startswith("replay gate: rejected:") and "turn 2" in err
    assert len(err.splitlines()) == 1


def test_recorded_fail_grade_and_bad_input(tmp_path, capsys):
    trace = _trace(tmp_path, "failure")
    assert check_record({"kind": "trace", "path": trace, "grade": "fail"}) is None
    assert main(["g", _rec(tmp_path, {"kind": "nope", "grade": "pass"})]) == EXIT_BAD_INPUT
    assert "bad record" in capsys.readouterr().err
    assert main(["g"]) == EXIT_BAD_INPUT


def test_incomplete_turn_is_not_graded_fail(tmp_path, capsys):
    """A pending/unknown step means the session was interrupted. The replay
    vocabulary says ``incomplete``; the gate must not collapse it into ``fail``
    and must not certify the record either."""
    path = _trace(tmp_path, "pending", name="interrupted.jsonl")
    # The verdict is surfaced under its own name, not as "fail".
    assert replay_grade({"kind": "trace", "path": path}) == ["incomplete"]
    for grade, name in (("pass", "p.json"), ("fail", "f.json")):
        rec = _rec(tmp_path, {"kind": "trace", "path": path, "grade": grade}, name)
        assert main(["g", rec]) == EXIT_NOT_REPLAYABLE
        err = capsys.readouterr().err.strip()
        assert err.startswith("replay gate: not replayable:"), err
        assert "replay grade fail" not in err
        assert "incomplete" in err


def test_zero_step_trace_is_not_graded_fail(tmp_path, capsys):
    """An empty window replays to no steps at all — its own verdict ``empty``."""
    path = _empty_trace(tmp_path)
    assert replay_grade({"kind": "trace", "path": path}) == ["empty"]
    rec = _rec(tmp_path, {"kind": "trace", "path": path, "grade": "pass"})
    assert main(["g", rec]) == EXIT_NOT_REPLAYABLE
    err = capsys.readouterr().err.strip()
    assert err.startswith("replay gate: not replayable:") and "empty" in err
    assert "replay grade fail" not in err


def test_torn_line_is_not_replayable(tmp_path, capsys):
    """A truncated second line used to be skipped, so one good turn certified
    the whole session. Strict loading makes the torn turn a hard error."""
    good = _trace(tmp_path, "success", name="torn.jsonl")
    text = Path(good).read_text(encoding="utf-8")
    Path(good).write_text(text + '{"session_id": "s2", "entries": [{"to', encoding="utf-8")
    rec = _rec(tmp_path, {"kind": "trace", "path": good, "grade": "pass"})
    assert main(["g", rec]) == EXIT_NOT_REPLAYABLE
    err = capsys.readouterr().err.strip()
    assert "not replayable" in err and "line 2" in err
    # Inspection (non-strict) still tolerates the torn write, as designed (#85).
    from evolution_trajectory_logger import iter_trajectories

    assert len(iter_trajectories(Path(good))) == 1


def test_unknown_outcome_is_not_guessed():
    """A new replay outcome must surface, not be guessed into pass/fail."""
    import pytest

    from evolution_replay_gate import _verdict

    with pytest.raises(NotReplayable, match="unknown replay outcome"):
        _verdict("teleported")


def test_shipped_record_reads_trace_at_head(tmp_path):
    """The merge path hands in the trace bytes read at the reviewed head SHA,
    not a local file — so the record is graded against exactly what lands."""
    body = _trace(tmp_path, "failure", name="body.jsonl")
    text = Path(body).read_text(encoding="utf-8")
    seen = []

    def read_text(path):
        seen.append(path)
        return {"evidence/turns.jsonl": text}.get(path)

    record = {"kind": "trace", "path": "evidence/turns.jsonl", "grade": "pass"}
    assert check_shipped_record(record, read_text=read_text, workdir=tmp_path) == [
        "REPLAY_NOT_REPRODUCED: turn 1 replay grade fail, recorded pass"
    ]
    assert seen == ["evidence/turns.jsonl"]

    ok = check_shipped_record(
        {"kind": "trace", "path": "evidence/turns.jsonl", "grade": "fail"},
        read_text=read_text,
        workdir=tmp_path,
    )
    assert ok == []


def test_shipped_record_missing_trace_or_record(tmp_path):
    record = {"kind": "trace", "path": "gone.jsonl", "grade": "pass"}
    out = check_shipped_record(record, read_text=lambda _p: None, workdir=tmp_path)
    assert out and out[0].startswith("REPLAY_UNVERIFIED:")
    assert check_shipped_record(
        {"kind": "trace", "grade": "pass"}, read_text=lambda _p: None
    )[0].startswith("REPLAY_UNVERIFIED:")
    assert check_shipped_record(
        {"kind": "weird", "grade": "pass"}, read_text=lambda _p: None
    )[0].startswith("REPLAY_UNVERIFIED:")
