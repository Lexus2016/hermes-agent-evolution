"""Tests for the #1703 patch replace-mode + empty old_string loop guard.

When the model calls the patch tool in replace mode with an EMPTY (falsy)
old_string, no valid replace can ever match — it is a usage error, not a match
failure. The guard returns a targeted, actionable diagnostic at the tool
boundary and tracks consecutive identical failures per task+path so the second
one escalates to a hard STOP — breaking the 5-8 call retry spiral at 2.
"""

import json

import pytest


@pytest.fixture(autouse=True)
def clear_trackers():
    """Reset the module-level trackers so counts start at zero regardless of
    prior test order."""
    from tools.file_tools import (
        _empty_old_string_lock,
        _empty_old_string_tracker,
        _patch_failure_lock,
        _patch_failure_tracker,
    )

    with _empty_old_string_lock:
        _empty_old_string_tracker.clear()
    with _patch_failure_lock:
        _patch_failure_tracker.clear()
    yield
    with _empty_old_string_lock:
        _empty_old_string_tracker.clear()
    with _patch_failure_lock:
        _patch_failure_tracker.clear()


class TestEmptyOldStringGuard:
    def _patch(self, task_id, path, old_string=None, new_string="x", mode="replace"):
        from tools.file_tools import _handle_patch

        args = {"mode": mode, "path": str(path), "new_string": new_string}
        if old_string is not None:
            args["old_string"] = old_string
        return json.loads(_handle_patch(args, task_id=task_id))

    def test_empty_old_string_returns_actionable_diagnostic(self, tmp_path):
        """An empty old_string in replace mode yields a targeted diagnostic
        naming the fix (non-empty old_string / mode=patch) — not the generic
        'old_string and new_string required'."""
        target = tmp_path / "f.py"
        target.write_text("def foo():\n    return 1\n")

        result = self._patch("t1", target, old_string="")
        assert result.get("error"), "expected an error envelope"
        err = result["error"]
        assert "non-empty old_string" in err, err
        assert "mode=patch" in err, err
        # The diagnostic is clearly non-retryable in intent.
        assert "cannot be matched" in err, err

    def test_omitted_old_string_also_guarded(self, tmp_path):
        """An omitted old_string (None) is treated the same as empty — the
        model passed replace mode without a search target."""
        target = tmp_path / "g.py"
        target.write_text("x = 1\n")

        result = self._patch("t2", target, old_string=None)
        assert result.get("error")
        assert "non-empty old_string" in result["error"]

    def test_second_consecutive_empty_escalates_to_stop(self, tmp_path):
        """The second consecutive empty-old_string failure on the same path
        escalates with a hard STOP — breaking the spiral at 2 instead of 8."""
        target = tmp_path / "h.py"
        target.write_text("y = 2\n")

        r1 = self._patch("t3", target, old_string="")
        assert "STOP" not in (r1.get("error") or ""), r1

        r2 = self._patch("t3", target, old_string="")
        assert "STOP" in (r2.get("error") or ""), r2
        assert "2nd consecutive" in (r2.get("error") or ""), r2

    def test_different_paths_independent_counters(self, tmp_path):
        """An empty-old_string failure on one path must not escalate a
        different path's counter."""
        a = tmp_path / "a.py"
        a.write_text("a = 1\n")
        b = tmp_path / "b.py"
        b.write_text("b = 2\n")

        self._patch("t4", a, old_string="")
        self._patch("t4", a, old_string="")  # a now at 2 → escalates

        r_b_first = self._patch("t4", b, old_string="")
        assert "STOP" not in (r_b_first.get("error") or ""), (
            "b's counter inherited a's escalation"
        )

    def test_non_empty_old_string_resets_empty_counter(self, tmp_path):
        """Once the model supplies a non-empty old_string (a real match), the
        empty-old_string counter for that path resets — a later empty call
        starts back at 1 (no STOP)."""
        target = tmp_path / "j.py"
        target.write_text("def foo():\n    return 1\n")

        self._patch("t5", target, old_string="")
        self._patch("t5", target, old_string="")  # streak 2
        # A valid replace: supplies a real old_string → resets the empty counter.
        r_valid = self._patch(
            "t5", target, old_string="return 1", new_string="return 99"
        )
        assert not r_valid.get("error"), r_valid

        r_after = self._patch("t5", target, old_string="")
        assert "STOP" not in (r_after.get("error") or ""), (
            "empty counter should have reset after a valid old_string"
        )
