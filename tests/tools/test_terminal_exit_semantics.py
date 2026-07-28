"""Tests for terminal command exit code semantic interpretation."""

import pytest

from tools.terminal_tool import _interpret_exit_code


class TestInterpretExitCode:
    """Test _interpret_exit_code returns correct notes for known command semantics."""

    # ---- exit code 0 always returns None ----

    def test_success_returns_none(self):
        assert _interpret_exit_code("grep foo bar", 0) is None
        assert _interpret_exit_code("diff a b", 0) is None
        assert _interpret_exit_code("test -f /etc/passwd", 0) is None

    # ---- grep / rg family: exit 1 = no matches ----

    @pytest.mark.parametrize("cmd", [
        "grep 'pattern' file.txt",
        "egrep 'pattern' file.txt",
        "fgrep 'pattern' file.txt",
        "rg 'foo' .",
        "ag 'foo' .",
        "ack 'foo' .",
    ])
    def test_grep_family_no_matches(self, cmd):
        result = _interpret_exit_code(cmd, 1)
        assert result is not None
        assert "no matches" in result.lower()

    def test_grep_real_error_no_note(self):
        """grep exit 2+ is a real error — should return None."""
        assert _interpret_exit_code("grep 'foo' bar", 2) is None
        assert _interpret_exit_code("rg 'foo' .", 2) is None

    # ---- diff: exit 1 = files differ ----

    def test_diff_files_differ(self):
        result = _interpret_exit_code("diff file1 file2", 1)
        assert result is not None
        assert "differ" in result.lower()

    def test_colordiff_files_differ(self):
        result = _interpret_exit_code("colordiff file1 file2", 1)
        assert result is not None
        assert "differ" in result.lower()

    def test_diff_real_error_no_note(self):
        assert _interpret_exit_code("diff a b", 2) is None

    # ---- test / [: exit 1 = condition false ----

    def test_test_condition_false(self):
        result = _interpret_exit_code("test -f /nonexistent", 1)
        assert result is not None
        assert "false" in result.lower()

    def test_bracket_condition_false(self):
        result = _interpret_exit_code("[ -f /nonexistent ]", 1)
        assert result is not None
        assert "false" in result.lower()

    # ---- find: exit 1 = partial success ----

    def test_find_partial_success(self):
        result = _interpret_exit_code("find . -name '*.py'", 1)
        assert result is not None
        assert "inaccessible" in result.lower()

    # ---- curl: various informational codes ----

    def test_curl_timeout(self):
        result = _interpret_exit_code("curl https://example.com", 28)
        assert result is not None
        assert "timed out" in result.lower()

    def test_curl_connection_refused(self):
        result = _interpret_exit_code("curl http://localhost:99999", 7)
        assert result is not None
        assert "connect" in result.lower()

    # ---- git: exit 1 is context-dependent ----

    def test_git_diff_exit_1(self):
        result = _interpret_exit_code("git diff HEAD~1", 1)
        assert result is not None
        assert "normal" in result.lower()

    # ---- pipeline / chain handling ----

    def test_pipeline_last_command(self):
        """In a pipeline, the last command determines the exit code."""
        result = _interpret_exit_code("ls -la | grep 'pattern'", 1)
        assert result is not None
        assert "no matches" in result.lower()

    def test_and_chain_last_command(self):
        result = _interpret_exit_code("cd /tmp && grep foo bar", 1)
        assert result is not None
        assert "no matches" in result.lower()

    def test_semicolon_chain_last_command(self):
        result = _interpret_exit_code("cat file; diff a b", 1)
        assert result is not None
        assert "differ" in result.lower()

    def test_or_chain_last_command(self):
        result = _interpret_exit_code("false || grep foo bar", 1)
        assert result is not None
        assert "no matches" in result.lower()

    # ---- full paths ----

    def test_full_path_command(self):
        result = _interpret_exit_code("/usr/bin/grep 'foo' bar", 1)
        assert result is not None
        assert "no matches" in result.lower()

    # ---- env var prefix ----

    def test_env_var_prefix_stripped(self):
        result = _interpret_exit_code("LANG=C grep 'foo' bar", 1)
        assert result is not None
        assert "no matches" in result.lower()

    def test_multiple_env_vars(self):
        result = _interpret_exit_code("FOO=1 BAR=2 grep 'foo' bar", 1)
        assert result is not None
        assert "no matches" in result.lower()

    # ---- unknown commands return None ----

    @pytest.mark.parametrize("cmd", [
        "python3 script.py",
        "rm -rf /tmp/test",
        "npm test",
        "make build",
        "cargo build",
    ])
    def test_unknown_commands_return_none(self, cmd):
        assert _interpret_exit_code(cmd, 1) is None

    # ---- edge cases ----

    def test_empty_command(self):
        assert _interpret_exit_code("", 1) is None

    def test_only_env_vars(self):
        """Command with only env var assignments, no actual command."""
        assert _interpret_exit_code("FOO=bar", 1) is None


class TestShellLevelExitCodes:
    """126/127 are about the INVOCATION, not the program (#1452).

    They are command-agnostic — 127 means the shell found nothing to run,
    whatever the command was — so they fell through the per-command table and
    produced no note at all. Worth naming because re-running unchanged cannot
    succeed, which is the retry-spiral shape behind terminal's 27.4% failure
    rate (#1371).
    """

    def test_127_names_the_cause_and_the_fix(self):
        note = _interpret_exit_code("nosuchtool --flag", 127)
        assert note is not None
        assert "not found" in note.lower()
        assert "path" in note.lower()

    def test_126_distinguishes_permission_from_missing(self):
        note = _interpret_exit_code("./script.sh", 126)
        assert note is not None
        assert "not executable" in note.lower()
        assert "not found" not in note.lower()

    def test_both_say_a_bare_retry_will_not_help(self):
        """The point is to stop the identical re-issue, so it has to be said."""
        for code in (126, 127):
            note = _interpret_exit_code("cmd", code)
            assert "unchanged" in note.lower()

    def test_shell_codes_apply_to_any_command(self):
        for cmd in ("foo", "git status", "curl https://x", "grep pattern file"):
            assert _interpret_exit_code(cmd, 127) is not None

    def test_command_specific_semantics_still_win(self):
        """A per-command meaning must not be shadowed by the new fallback."""
        assert "No matches" in _interpret_exit_code("grep x f", 1)
        assert "connect" in _interpret_exit_code("curl https://x", 7)

    def test_signal_codes_still_win(self):
        assert "SIGKILL" in _interpret_exit_code("sleep 1", 137)

    def test_unknown_code_still_returns_none(self):
        assert _interpret_exit_code("mycmd", 3) is None

    def test_zero_still_returns_none(self):
        assert _interpret_exit_code("nosuchtool", 0) is None
