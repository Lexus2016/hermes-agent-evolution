"""Tests for per-file consecutive patch-failure tracking.

When the agent repeatedly fails to patch the same file with similar but
non-matching old_strings, it's usually stuck in a loop with a stale view
of the file.  After 3 consecutive failures on the same path, the patch
tool injects an escalating ``_hint`` that tells the model to break out
of the loop (re-read, use longer context, or fall back to write_file).

See issue #507 (Roo Code deep-dive, item 2f).
"""

import json

import pytest


@pytest.fixture
def hermes_home(monkeypatch, tmp_path):
    """Isolate HERMES_HOME and clear module-level caches afterward so the
    real shell-out side effects from _handle_patch don't leak into
    subsequent tests (see test_line_ending_preservation.py for details)."""
    home = tmp_path / "hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    yield home
    try:
        from tools.file_tools import (
            clear_file_ops_cache,
            _read_tracker_lock,
            _read_tracker,
        )

        clear_file_ops_cache()
        with _read_tracker_lock:
            _read_tracker.clear()
    except Exception:
        pass
    try:
        from tools.terminal_tool import _active_environments, _env_lock

        with _env_lock:
            _active_environments.clear()
    except Exception:
        pass


@pytest.fixture
def fresh_tracker():
    """Reset the module-level tracker before each test so the count starts
    at zero regardless of prior test order."""
    from tools.file_tools import _patch_failure_tracker, _patch_failure_lock

    with _patch_failure_lock:
        _patch_failure_tracker.clear()
    yield
    with _patch_failure_lock:
        _patch_failure_tracker.clear()


class TestPatchFailureEscalation:
    def test_first_two_failures_no_hard_escalation(
        self, hermes_home, tmp_path, fresh_tracker
    ):
        from tools.file_tools import _handle_patch

        target = tmp_path / "f.py"
        target.write_text("def foo():\n    return 1\n")

        for _i in range(2):
            result = _handle_patch(
                {
                    "mode": "replace",
                    "path": str(target),
                    "old_string": f"NONEXISTENT_{_i}_XYZQQQ",
                    "new_string": "x",
                },
                task_id="esc_t1",
            )
            d = json.loads(result)
            hint = d.get("_hint", "") or ""
            # The hard "PERMANENT FAILURE" escalation must not fire before the
            # 3rd consecutive failure (#507). Note: a softer write_file nudge
            # MAY appear on the 2nd no-match failure (#1537) — that's tested
            # separately in test_second_consecutive_no_match_nudges_write_file.
            assert "PERMANENT FAILURE" not in hint, (
                f"Hard escalation fired too early on attempt {_i + 1}: {hint!r}"
            )

    def test_third_consecutive_failure_escalates(
        self, hermes_home, tmp_path, fresh_tracker
    ):
        from tools.file_tools import _handle_patch

        target = tmp_path / "f.py"
        target.write_text("def foo():\n    return 1\n")

        last_hint = ""
        for _i in range(3):
            result = _handle_patch(
                {
                    "mode": "replace",
                    "path": str(target),
                    "old_string": f"DOES_NOT_EXIST_{_i}_FOOFOOFOO",
                    "new_string": "x",
                },
                task_id="esc_t2",
            )
            d = json.loads(result)
            last_hint = d.get("_hint", "") or ""

        assert "failure #3" in last_hint, repr(last_hint)
        assert "PERMANENT FAILURE" in last_hint, (
            "Escalating hint should classify as PERMANENT FAILURE"
        )
        assert "Stop retrying" in last_hint
        assert "write_file" in last_hint, (
            "Escalating hint should mention write_file fallback"
        )

    def test_second_consecutive_no_match_nudges_write_file(
        self, hermes_home, tmp_path, fresh_tracker
    ):
        """On the 2nd consecutive no-match failure on the same file, the patch
        tool surfaces a write_file-forward nudge BEFORE the hard 3rd-failure
        refuse-gate fires (#1537). The introspection evidence showed the
        model keeps retrying near-identical old_string variants past the
        point a full rewrite would have been faster; surfacing write_file
        early biases it toward the reliably-terminating path. The hint puts
        write_file FIRST and must appear on attempt 2 but not attempt 1."""
        from tools.file_tools import _handle_patch

        target = tmp_path / "g.py"
        target.write_text("def foo():\n    return 1\n")

        # Attempt 1: generic no-match hint, no write_file-forward nudge yet.
        result = _handle_patch(
            {
                "mode": "replace",
                "path": str(target),
                "old_string": "GHOST_ATTEMPT1_QQQ",
                "new_string": "x",
            },
            task_id="esc_t1b",
        )
        d1 = json.loads(result)
        hint1 = d1.get("_hint", "") or ""
        assert "switch to write_file" not in hint1, (
            f"write_file nudge fired too early on attempt 1: {hint1!r}"
        )

        # Attempt 2: the #1537 write_file-forward nudge now appears.
        result = _handle_patch(
            {
                "mode": "replace",
                "path": str(target),
                "old_string": "GHOST_ATTEMPT2_QQQ",
                "new_string": "x",
            },
            task_id="esc_t1b",
        )
        d2 = json.loads(result)
        hint2 = d2.get("_hint", "") or ""
        assert "failure #2" in hint2, repr(hint2)
        assert "write_file" in hint2, (
            f"2nd-failure nudge should recommend write_file: {hint2!r}"
        )
        # Must NOT yet be the hard refuse-gate message.
        assert "PERMANENT FAILURE" not in hint2

    def test_success_clears_failure_counter(self, hermes_home, tmp_path, fresh_tracker):
        from tools.file_tools import _handle_patch

        target = tmp_path / "f.py"
        target.write_text("def foo():\n    return 1\n")

        # Three failures: counter at 3.
        for _i in range(3):
            _handle_patch(
                {
                    "mode": "replace",
                    "path": str(target),
                    "old_string": f"GHOST_{_i}_ABCABC",
                    "new_string": "x",
                },
                task_id="esc_t3",
            )

        # Successful patch: clears the counter.
        result = _handle_patch(
            {
                "mode": "replace",
                "path": str(target),
                "old_string": "return 1",
                "new_string": "return 99",
            },
            task_id="esc_t3",
        )
        d = json.loads(result)
        assert not d.get("error"), d

        # Next failure should be back to "attempt 1" — generic hint only.
        result = _handle_patch(
            {
                "mode": "replace",
                "path": str(target),
                "old_string": "STILL_GHOST_XYZ",
                "new_string": "x",
            },
            task_id="esc_t3",
        )
        d = json.loads(result)
        hint = d.get("_hint", "") or ""
        assert "failure #" not in hint, (
            f"Counter should have been reset after success: {hint!r}"
        )

    def test_different_paths_have_independent_counters(
        self, hermes_home, tmp_path, fresh_tracker
    ):
        from tools.file_tools import _handle_patch

        a = tmp_path / "a.py"
        a.write_text("x = 1\n")
        b = tmp_path / "b.py"
        b.write_text("y = 2\n")

        # Three failures on a.py.
        for _i in range(3):
            _handle_patch(
                {
                    "mode": "replace",
                    "path": str(a),
                    "old_string": f"NONE_A_{_i}_ZZZ",
                    "new_string": "x",
                },
                task_id="esc_t4",
            )

        # One failure on b.py — should NOT inherit a.py's count.
        result = _handle_patch(
            {
                "mode": "replace",
                "path": str(b),
                "old_string": "NONE_B_ZZZ",
                "new_string": "x",
            },
            task_id="esc_t4",
        )
        d = json.loads(result)
        hint = d.get("_hint", "") or ""
        assert "failure #" not in hint, f"b.py's hint inherited a.py's count: {hint!r}"

    def test_different_tasks_have_independent_counters(
        self, hermes_home, tmp_path, fresh_tracker
    ):
        from tools.file_tools import _handle_patch

        target = tmp_path / "shared.py"
        target.write_text("z = 0\n")

        # Three failures under task A.
        for _i in range(3):
            _handle_patch(
                {
                    "mode": "replace",
                    "path": str(target),
                    "old_string": f"GHOST_A_{_i}_QWE",
                    "new_string": "x",
                },
                task_id="task_A",
            )

        # First failure under task B — should NOT see escalation.
        result = _handle_patch(
            {
                "mode": "replace",
                "path": str(target),
                "old_string": "GHOST_B_QWE",
                "new_string": "x",
            },
            task_id="task_B",
        )
        d = json.loads(result)
        hint = d.get("_hint", "") or ""
        assert "failure #" not in hint, (
            f"task_B's hint cross-contaminated from task_A: {hint!r}"
        )
