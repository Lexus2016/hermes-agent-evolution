"""Tests for per-tool non-retryable failure classification (#1612).

The global ``_NON_RETRYABLE`` set covers deterministic classes for EVERY
tool (timeout / permission / missing_command / limit). #1612 extends this
with PER-TOOL deterministic classes: a ``not_found`` on ``read_file`` for
the same path is permanent, and a ``not_found`` on ``patch`` (absent anchor/
file) is permanent for that anchor. ``terminal`` ``not_found`` must REMAIN
retryable — a missing path on a shell command can be a transient PATH issue.
``search_files`` no-results stays retryable — an absent result is legitimately
retried with a broadened query.
"""

from agent.loop_guard import (
    _is_non_retryable,
    maybe_nudge,
    run_warrants_cron_hard_stop,
)


def _fail_run(tool, n, result):
    """``n`` consecutive single-tool turns returning the same failure."""
    msgs = [{"role": "user", "content": "go"}]
    for i in range(n):
        cid = f"c{i}"
        msgs.append({
            "role": "assistant",
            "tool_calls": [{"id": cid, "function": {"name": tool, "arguments": "{}"}}],
        })
        msgs.append({"role": "tool", "tool_call_id": cid, "content": result})
    return msgs


class TestIsNonRetryable:
    def test_read_file_not_found_is_non_retryable(self):
        assert _is_non_retryable("read_file", "not_found")

    def test_patch_not_found_is_non_retryable(self):
        assert _is_non_retryable("patch", "not_found")

    def test_terminal_not_found_stays_retryable(self):
        # Missing path on a shell command can be a transient PATH issue.
        assert not _is_non_retryable("terminal", "not_found")

    def test_search_files_not_found_stays_retryable(self):
        # "no matches" is legitimately retried with a broadened query.
        assert not _is_non_retryable("search_files", "not_found")

    def test_unknown_tool_not_found_stays_retryable(self):
        assert not _is_non_retryable("some_mcp_tool", "not_found")

    def test_global_classes_still_apply(self):
        assert _is_non_retryable("read_file", "permission")
        assert _is_non_retryable("patch", "timeout")
        assert _is_non_retryable("terminal", "missing_command")
        assert _is_non_retryable("weird_tool", "limit")

    def test_none_category_is_false(self):
        assert not _is_non_retryable("read_file", None)


class TestReadFileNotFoundNonRetryable:
    def test_two_not_found_trips_hard_stop(self):
        n = maybe_nudge(_fail_run("read_file", 2, "Error: file does not exist"))
        assert n is not None and "non-retryable" in n and "not_found" in n

    def test_two_not_found_warrants_cron_hard_stop(self):
        assert run_warrants_cron_hard_stop(
            _fail_run("read_file", 2, "Error: file does not exist")
        )

    def test_single_not_found_is_quiet(self):
        assert maybe_nudge(_fail_run("read_file", 1, "Error: file does not exist")) is None

    def test_nudge_appends_fallback_hint(self):
        n = maybe_nudge(_fail_run("read_file", 2, "Error: file does not exist"))
        assert n is not None and "repo_map" in n


class TestPatchNotFoundNonRetryable:
    def test_two_not_found_trips_hard_stop(self):
        n = maybe_nudge(_fail_run("patch", 2, "Error: match not found in file"))
        assert n is not None and "non-retryable" in n and "not_found" in n

    def test_two_not_found_warrants_cron_hard_stop(self):
        assert run_warrants_cron_hard_stop(
            _fail_run("patch", 2, "Error: match not found in file")
        )

    def test_nudge_appends_fallback_hint(self):
        n = maybe_nudge(_fail_run("patch", 2, "Error: match not found in file"))
        assert n is not None and "read_file" in n  # patch hint: re-read target


class TestTerminalNotFoundRemainsRetryable:
    def test_two_not_found_falls_through_to_generic_fail(self):
        n = maybe_nudge(
            _fail_run("terminal", 2, "ls: cannot access foo: No such file or directory")
        )
        assert n is not None and "non-retryable" not in n and "failed 2 times" in n

    def test_two_not_found_warrants_cron_hard_stop_via_generic_fail(self):
        assert run_warrants_cron_hard_stop(
            _fail_run("terminal", 2, "ls: cannot access foo: No such file or directory")
        )


class TestSearchFilesNoResultStaysRetryable:
    def test_two_no_result_is_quiet(self):
        # provider_dead is not non-retryable — absent result is retried.
        assert maybe_nudge(_fail_run("search_files", 2, "no results found")) is None

    def test_three_no_result_fires_generic_fail(self):
        n = maybe_nudge(_fail_run("search_files", 3, "no results found"))
        assert n is not None and "non-retryable" not in n


class TestGlobalClassesStillApplyToPerToolTools:
    def test_read_file_permission_trips_non_retryable(self):
        n = maybe_nudge(_fail_run("read_file", 2, "permission denied"))
        assert n is not None and "non-retryable" in n and "permission" in n

    def test_patch_timeout_trips_non_retryable(self):
        n = maybe_nudge(_fail_run("patch", 2, "failure-class=timeout — timed out"))
        assert n is not None and "non-retryable" in n and "timeout" in n
