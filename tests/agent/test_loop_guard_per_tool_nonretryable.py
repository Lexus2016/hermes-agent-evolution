"""Tests for per-tool non-retryable failure classification (#1612).

The global ``_NON_RETRYABLE`` set covers deterministic failure classes that
apply to EVERY tool (timeout / permission / missing_command / limit). Issue
#1612 extends this with PER-TOOL deterministic classes: a ``not_found`` on
``read_file`` for the same path is permanent (the file won't appear on
retry), and a ``not_found`` on ``patch`` (the anchor/file the patch targets
is absent) is permanent for that same anchor. ``terminal`` ``not_found``
must REMAIN retryable — a missing path on a shell command can be a transient
PATH issue and the agent may legitimately fix the cwd/path and retry.

These tests assert the union semantics:
  * read_file/patch ``not_found`` x2 trips the deterministic non-retryable
    hard stop (below the generic fail threshold).
  * terminal ``not_found`` x2 does NOT trip as non-retryable — it falls
    through to the generic fail path.
  * global classes (permission) still trip for read_file.
  * search_files ``no matches`` (classifies ``not_found``) stays retryable —
    an absent symbol is legitimately retried with a broadened query, so a
    hard non-retryable stop would over-block.
"""

from agent.loop_guard import (
    _is_non_retryable,
    maybe_nudge,
    run_warrants_cron_hard_stop,
)


def _asst(tool, args="{}", call_id="c"):
    return {
        "role": "assistant",
        "tool_calls": [{"id": call_id, "function": {"name": tool, "arguments": args}}],
    }


def _result(content, call_id="c"):
    return {"role": "tool", "tool_call_id": call_id, "content": content}


def _fail_run(tool, n, result):
    """``n`` consecutive single-tool turns, each returning the same failure."""
    msgs = [{"role": "user", "content": "go"}]
    for i in range(n):
        cid = f"c{i}"
        msgs.append(_asst(tool, call_id=cid))
        msgs.append(_result(result, call_id=cid))
    return msgs


class TestIsNonRetryable:
    def test_read_file_not_found_is_non_retryable(self):
        assert _is_non_retryable("read_file", "not_found")

    def test_patch_not_found_is_non_retryable(self):
        assert _is_non_retryable("patch", "not_found")

    def test_terminal_not_found_stays_retryable(self):
        # A missing path on a shell command can be a transient PATH issue —
        # the agent may legitimately fix cwd/path and retry. Not in the
        # per-tool set, so it must NOT count as deterministic.
        assert not _is_non_retryable("terminal", "not_found")

    def test_search_files_not_found_stays_retryable(self):
        # "no matches" on a search is legitimately retried with a broadened
        # query — not a hard non-retryable stop.
        assert not _is_non_retryable("search_files", "not_found")

    def test_unknown_tool_not_found_stays_retryable(self):
        assert not _is_non_retryable("some_mcp_tool", "not_found")

    def test_global_classes_still_non_retryable(self):
        # Global classes apply to every tool, including read_file/patch.
        assert _is_non_retryable("read_file", "permission")
        assert _is_non_retryable("patch", "timeout")
        assert _is_non_retryable("terminal", "missing_command")
        assert _is_non_retryable("weird_tool", "limit")

    def test_none_category_is_false(self):
        assert not _is_non_retryable("read_file", None)


class TestReadFileNotFoundNonRetryable:
    def test_two_not_found_trips_hard_stop(self):
        # read_file not_found x2 is deterministic -> the non-retryable hard
        # stop fires (below the generic idempotent fail threshold of 3).
        n = maybe_nudge(_fail_run("read_file", 2, "Error: file does not exist"))
        assert n is not None and "non-retryable" in n and "not_found" in n

    def test_two_not_found_warrants_cron_hard_stop(self):
        assert run_warrants_cron_hard_stop(
            _fail_run("read_file", 2, "Error: file does not exist")
        )

    def test_single_not_found_is_quiet(self):
        # One deterministic failure is not yet a spiral.
        assert (
            maybe_nudge(_fail_run("read_file", 1, "Error: file does not exist")) is None
        )

    def test_nudge_appends_diversion_hint(self):
        # The non-retryable nudge must surface the fallback hint (repo_map /
        # delegate_task) so the model switches strategy instead of re-reading.
        n = maybe_nudge(_fail_run("read_file", 2, "Error: file does not exist"))
        assert n is not None
        assert "repo_map" in n


class TestPatchNotFoundNonRetryable:
    def test_two_not_found_trips_hard_stop(self):
        # patch not_found x2 is deterministic (mutating tools already trip the
        # generic fail path at 2, but this should fire as the non-retryable
        # class with the diversion hint, not the generic wording).
        n = maybe_nudge(_fail_run("patch", 2, "Error: match not found in file"))
        assert n is not None and "non-retryable" in n and "not_found" in n

    def test_two_not_found_warrants_cron_hard_stop(self):
        assert run_warrants_cron_hard_stop(
            _fail_run("patch", 2, "Error: match not found in file")
        )

    def test_nudge_appends_diversion_hint(self):
        n = maybe_nudge(_fail_run("patch", 2, "Error: match not found in file"))
        assert n is not None
        assert "read_file" in n  # patch's diversion hint says re-read the target


class TestTerminalNotFoundRemainsRetryable:
    def test_two_not_found_falls_through_to_generic_fail(self):
        # terminal not_found x2: NOT the non-retryable path. It trips the
        # generic fail branch (mutating fail_threshold=2), with the generic
        # "failed N times" wording, NOT "non-retryable".
        n = maybe_nudge(
            _fail_run("terminal", 2, "ls: cannot access foo: No such file or directory")
        )
        assert n is not None
        assert "non-retryable" not in n
        assert "failed 2 times" in n

    def test_two_not_found_warrants_cron_hard_stop_via_generic_fail(self):
        # It still warrants a cron hard stop (genuine failing spiral) but via
        # the generic consecutive-failure path, not the deterministic path.
        assert run_warrants_cron_hard_stop(
            _fail_run("terminal", 2, "ls: cannot access foo: No such file or directory")
        )

    def test_one_not_found_is_quiet(self):
        # terminal mutating fail_threshold is 2; a single failure stays quiet.
        assert (
            maybe_nudge(
                _fail_run(
                    "terminal", 1, "ls: cannot access foo: No such file or directory"
                )
            )
            is None
        )


class TestGlobalClassesStillApplyToPerToolTools:
    def test_read_file_permission_trips_non_retryable(self):
        n = maybe_nudge(_fail_run("read_file", 2, "permission denied"))
        assert n is not None and "non-retryable" in n and "permission" in n

    def test_patch_timeout_trips_non_retryable(self):
        n = maybe_nudge(_fail_run("patch", 2, "failure-class=timeout — timed out"))
        assert n is not None and "non-retryable" in n and "timeout" in n


class TestSearchFilesNoResultStaysRetryable:
    def test_two_no_result_is_quiet(self):
        # search_files "no results found" is a detected failure (classifies as
        # provider_dead, NOT not_found) but must NOT trip the deterministic
        # stop at 2 — provider_dead is not in any non-retryable set, and an
        # absent result is legitimately retried with a different query/provider.
        n = maybe_nudge(_fail_run("search_files", 2, "no results found"))
        assert n is None

    def test_three_no_result_fires_generic_fail(self):
        # At 3 (the per-tool search_files fail threshold from #973), the
        # generic fail nudge fires — NOT the non-retryable wording.
        n = maybe_nudge(_fail_run("search_files", 3, "no results found"))
        assert n is not None
        assert "non-retryable" not in n
