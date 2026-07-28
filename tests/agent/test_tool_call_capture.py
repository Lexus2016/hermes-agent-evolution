# -*- coding: utf-8 -*-
"""Tests for real tool-call capture (issue #1363)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

import pytest  # noqa: E402

from agent.tool_call_capture import (  # noqa: E402
    _CAPTURE_ENV,
    build_trajectory_log,
    capture_enabled,
    capture_turn,
    extract_tool_calls,
    task_key,
)


@pytest.fixture
def capture_on(monkeypatch):
    """Capture is opt-in (#1363 review): tests that exercise it must ask."""
    monkeypatch.setenv(_CAPTURE_ENV, "1")


def _call(name, args, cid):
    return {
        "role": "assistant",
        "tool_calls": [
            {"id": cid, "type": "function",
             "function": {"name": name, "arguments": json.dumps(args)}}
        ],
    }


def _result(cid, content):
    return {"role": "tool", "tool_call_id": cid, "content": content}


def _ok(cid):
    return _result(cid, json.dumps({"success": True}))


def _fail(cid):
    return _result(cid, json.dumps({"error": "no such file"}))


class TestTaskKey:
    def test_stable_for_same_text(self, tmp_path):
        assert task_key("fix the parser", tmp_path) == task_key("fix the parser", tmp_path)

    def test_differs_for_different_text(self, tmp_path):
        assert task_key("fix the parser", tmp_path) != task_key("fix the linter", tmp_path)

    def test_empty_is_empty(self, tmp_path):
        assert task_key("", tmp_path) == ""

    def test_does_not_leak_the_descriptor(self, tmp_path):
        """A pairing key, not a record of the prompt."""
        secret = "deploy to prod with password hunter2"
        key = task_key(secret, tmp_path)
        assert "hunter2" not in key
        assert "deploy" not in key
        assert len(key) == 16

    def test_salted_per_store(self, tmp_path):
        """A bare sha256 of a short templated prompt is recoverable from a
        rainbow table; an HMAC under a host-local salt is not. Two stores must
        therefore not produce the same key for the same text."""
        a, b = tmp_path / "a", tmp_path / "b"
        a.mkdir(); b.mkdir()
        assert task_key("run the tests", a) != task_key("run the tests", b)

    def test_salt_is_reused_within_a_store(self, tmp_path):
        """...but pairing within one store must still work across calls."""
        first = task_key("run the tests", tmp_path)
        assert task_key("run the tests", tmp_path) == first

    def test_salt_file_is_private(self, tmp_path):
        task_key("x", tmp_path)
        salt = tmp_path / ".trajectory_key_salt"
        assert salt.exists()
        assert oct(salt.stat().st_mode)[-3:] == "600"


class TestExtractToolCalls:
    def test_pairs_calls_with_results(self):
        msgs = [_call("read_file", {"path": "a.py"}, "c1"), _ok("c1")]
        calls = extract_tool_calls(msgs)
        assert len(calls) == 1
        assert calls[0]["tool"] == "read_file"
        assert calls[0]["status"] == "success"

    def test_classifies_failure(self):
        msgs = [_call("read_file", {"path": "nope"}, "c1"), _fail("c1")]
        assert extract_tool_calls(msgs)[0]["status"] == "failure"

    def test_call_without_result_is_pending_not_dropped(self):
        """A call that never returned is signal for #1268's error-recovery
        dimension, so it must survive rather than vanish."""
        calls = extract_tool_calls([_call("terminal", {"command": "sleep"}, "c1")])
        assert len(calls) == 1
        assert calls[0]["status"] == "pending"

    def test_preserves_order(self):
        msgs = [
            _call("read_file", {}, "c1"), _ok("c1"),
            _call("patch", {}, "c2"), _ok("c2"),
            _call("terminal", {}, "c3"), _ok("c3"),
        ]
        assert [c["tool"] for c in extract_tool_calls(msgs)] == ["read_file", "patch", "terminal"]

    def test_multi_tool_turn(self):
        msgs = [{
            "role": "assistant",
            "tool_calls": [
                {"id": "a", "function": {"name": "read_file", "arguments": "{}"}},
                {"id": "b", "function": {"name": "search_files", "arguments": "{}"}},
            ],
        }, _ok("a"), _ok("b")]
        assert len(extract_tool_calls(msgs)) == 2

    def test_malformed_input_does_not_raise(self):
        for bad in (None, "string", 42, [None], [{"role": "assistant", "tool_calls": "x"}],
                    [{"role": "assistant", "tool_calls": [None]}],
                    [{"role": "assistant", "tool_calls": [{"function": None}]}],
                    [{"role": "assistant", "tool_calls": [{"function": {}}]}]):
            assert extract_tool_calls(bad) == []

    def test_unparseable_arguments_become_empty_dict(self):
        msgs = [{
            "role": "assistant",
            "tool_calls": [{"id": "c1", "function": {"name": "t", "arguments": "not json"}}],
        }, _ok("c1")]
        assert extract_tool_calls(msgs)[0]["args"] == {}

    def test_no_tool_calls_returns_empty(self):
        assert extract_tool_calls([{"role": "assistant", "content": "just text"}]) == []


class TestBuildTrajectoryLog:
    def test_carries_outcome_and_pairing_key(self, tmp_path):
        msgs = [_call("read_file", {"path": "a.py"}, "c1"), _ok("c1")]
        log = build_trajectory_log(msgs, session_id="s1", task_descriptor="fix it", completed=True, trajectory_dir=tmp_path)
        assert log is not None
        assert log.completed is True
        assert log.task_key == task_key("fix it", tmp_path)
        assert log.session_id == "s1"

    def test_failed_turn_records_completed_false(self):
        """#1359 filters on this — a heuristic is only worth distilling from a
        trajectory whose outcome is known."""
        msgs = [_call("terminal", {}, "c1"), _fail("c1")]
        log = build_trajectory_log(msgs, completed=False)
        assert log.completed is False

    def test_same_task_different_outcomes_share_a_key(self, tmp_path):
        """This is what #1436 pairs on."""
        msgs = [_call("terminal", {}, "c1"), _ok("c1")]
        a = build_trajectory_log(msgs, task_descriptor="same task", completed=False, trajectory_dir=tmp_path)
        b = build_trajectory_log(msgs, task_descriptor="same task", completed=True, trajectory_dir=tmp_path)
        assert a.task_key == b.task_key
        assert a.completed != b.completed

    def test_turn_with_no_tool_calls_is_none(self):
        """An empty trajectory tells every consumer nothing and would only
        dilute the store."""
        assert build_trajectory_log([{"role": "assistant", "content": "hi"}]) is None

    def test_arguments_are_redacted(self):
        msgs = [_call("api", {"token": "secret-value", "path": "a.py"}, "c1"), _ok("c1")]
        log = build_trajectory_log(msgs)
        arg = log.entries[0].args_summary
        assert arg["token"] == "[REDACTED]"
        assert arg["path"] == "a.py"


class TestCaptureTurn:
    def test_writes_a_readable_trajectory(self, tmp_path, capture_on):
        msgs = [_call("read_file", {"path": "a.py"}, "c1"), _ok("c1")]
        path = capture_turn(msgs, session_id="s1", task_descriptor="t", completed=True,
                            trajectory_dir=tmp_path)
        assert path is not None and path.exists()
        data = json.loads(path.read_text(encoding="utf-8").strip())
        assert data["session_id"] == "s1"
        assert data["completed"] is True
        assert data["task_key"] == task_key("t", tmp_path)
        assert [e["tool"] for e in data["entries"]] == ["read_file"]

    def test_no_tool_calls_writes_nothing(self, tmp_path, capture_on):
        assert capture_turn([{"role": "assistant", "content": "hi"}], trajectory_dir=tmp_path) is None
        assert list(tmp_path.glob("*.jsonl")) == []

    def test_never_raises_on_bad_input(self, tmp_path, capture_on):
        assert capture_turn(None, trajectory_dir=tmp_path) is None

    def test_never_raises_on_unwritable_dir(self, tmp_path, capture_on):
        """Instrumentation must not be able to discard a completed turn."""
        blocker = tmp_path / "file"
        blocker.write_text("x", encoding="utf-8")
        msgs = [_call("read_file", {}, "c1"), _ok("c1")]
        assert capture_turn(msgs, trajectory_dir=blocker / "nested") is None

    def test_no_user_prose_reaches_disk(self, tmp_path, capture_on):
        """The reason this can run while save_trajectories stays off."""
        msgs = [
            {"role": "user", "content": "my private prompt about acme corp"},
            _call("read_file", {"path": "a.py"}, "c1"),
            _result("c1", "file contents mentioning acme corp"),
            {"role": "assistant", "content": "here is my private analysis"},
        ]
        path = capture_turn(msgs, task_descriptor="my private prompt about acme corp",
                            trajectory_dir=tmp_path)
        raw = path.read_text(encoding="utf-8")
        assert "my private prompt" not in raw
        assert "private analysis" not in raw


class TestLoggerBackCompat:
    """The pre-#1363 cron-stage shape must round-trip unchanged."""

    def test_old_shape_omits_new_keys(self, tmp_path):
        from evolution_trajectory_logger import TrajectoryLog

        log = TrajectoryLog(session_id="cron")
        log.add_tool_call("evolution_funnel", {"date": "2026-07-28"}, result={}, status="success")
        data = json.loads(log.to_json())
        assert "completed" not in data
        assert "task_key" not in data

    def test_old_file_loads_with_unset_fields(self, tmp_path):
        from evolution_trajectory_logger import load_trajectory

        p = tmp_path / "old.json"
        p.write_text(json.dumps({
            "date": "2026-07-01", "session_id": "cron",
            "entries": [{"tool": "evolution_funnel", "args_summary": {},
                         "result_status": "success", "result_summary": "ok"}],
        }), encoding="utf-8")
        log = load_trajectory(p)
        assert log is not None
        assert log.completed is None, "absent must mean 'not recorded', not 'failed'"
        assert log.task_key == ""

    def test_new_file_round_trips(self, tmp_path, capture_on):
        msgs = [_call("read_file", {}, "c1"), _ok("c1")]
        path = capture_turn(msgs, session_id="s", task_descriptor="t", completed=False,
                            trajectory_dir=tmp_path)
        line = path.read_text(encoding="utf-8").strip()
        data = json.loads(line)
        assert data["completed"] is False
        assert data["task_key"] == task_key("t", tmp_path)


class TestCaptureIsOptIn:
    """Default OFF (#1363 review). Redaction catches credential-shaped keys,
    not sensitive values in ordinary fields, so writing on every turn of every
    interactive session unasked is the wrong default even for metadata."""

    def test_disabled_by_default(self, tmp_path, monkeypatch):
        monkeypatch.delenv(_CAPTURE_ENV, raising=False)
        assert capture_enabled() is False
        msgs = [_call("read_file", {}, "c1"), _ok("c1")]
        assert capture_turn(msgs, trajectory_dir=tmp_path) is None
        assert list(tmp_path.glob("*.jsonl")) == []

    def test_enabled_by_env(self, tmp_path, monkeypatch):
        for value in ("1", "true", "yes", "on", "ON"):
            monkeypatch.setenv(_CAPTURE_ENV, value)
            assert capture_enabled() is True, value

    def test_junk_value_does_not_enable(self, monkeypatch):
        for value in ("0", "false", "no", "", "maybe"):
            monkeypatch.setenv(_CAPTURE_ENV, value)
            assert capture_enabled() is False, value


class TestMultiTurnSessionsAppend:
    """save() overwrites; a multi-turn session would keep only its last turn.

    Confirmed before the fix: three turns of one session left one file with
    only the third turn's calls. The action *sequence* is exactly what the
    consumers of #1363 want, so losing it defeats the feature.
    """

    def test_every_turn_is_kept(self, tmp_path, capture_on):
        for tool, cid in (("read_file", "a"), ("patch", "b"), ("terminal", "c")):
            capture_turn([_call(tool, {}, cid), _ok(cid)], session_id="s1",
                         task_descriptor="t", trajectory_dir=tmp_path)
        files = list(tmp_path.glob("*.jsonl"))
        assert len(files) == 1
        lines = [json.loads(x) for x in files[0].read_text(encoding="utf-8").splitlines() if x.strip()]
        assert [line["entries"][0]["tool"] for line in lines] == ["read_file", "patch", "terminal"]

    def test_separate_sessions_get_separate_files(self, tmp_path, capture_on):
        capture_turn([_call("read_file", {}, "a"), _ok("a")], session_id="s1", trajectory_dir=tmp_path)
        capture_turn([_call("patch", {}, "b"), _ok("b")], session_id="s2", trajectory_dir=tmp_path)
        assert len(list(tmp_path.glob("*.jsonl"))) == 2

    def test_each_line_is_independently_parseable(self, tmp_path, capture_on):
        """A reader must be able to take one turn at a time without holding
        the session in memory."""
        for cid in ("a", "b"):
            capture_turn([_call("terminal", {}, cid), _ok(cid)], session_id="s1",
                         trajectory_dir=tmp_path)
        path = list(tmp_path.glob("*.jsonl"))[0]
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                assert json.loads(line)["entries"]
