"""Regression tests for sudo detection and sudo password handling."""

import json
import tools.terminal_tool as terminal_tool
import tools.terminal_tool_sudo as terminal_tool_sudo
import tools.terminal_tool_guards as terminal_tool_guards


def setup_function():
    terminal_tool_sudo._reset_cached_sudo_passwords()


def teardown_function():
    terminal_tool_sudo._reset_cached_sudo_passwords()


def test_searching_for_sudo_does_not_trigger_rewrite(monkeypatch):
    monkeypatch.delenv("SUDO_PASSWORD", raising=False)
    monkeypatch.delenv("HERMES_INTERACTIVE", raising=False)

    command = "rg --line-number --no-heading --with-filename 'sudo' . | head -n 20"
    transformed, sudo_stdin = terminal_tool_sudo._transform_sudo_command(command)

    assert transformed == command
    assert sudo_stdin is None


def test_terminal_schema_advertises_persistent_env_state():
    description = terminal_tool.TERMINAL_TOOL_DESCRIPTION

    assert "exported environment variables persist between calls" in description
    assert "activate a virtualenv" in description
    assert "once per session" in description


def test_printf_literal_sudo_does_not_trigger_rewrite(monkeypatch):
    monkeypatch.delenv("SUDO_PASSWORD", raising=False)
    monkeypatch.delenv("HERMES_INTERACTIVE", raising=False)

    command = "printf '%s\\n' sudo"
    transformed, sudo_stdin = terminal_tool_sudo._transform_sudo_command(command)

    assert transformed == command
    assert sudo_stdin is None


def test_non_command_argument_named_sudo_does_not_trigger_rewrite(monkeypatch):
    monkeypatch.delenv("SUDO_PASSWORD", raising=False)
    monkeypatch.delenv("HERMES_INTERACTIVE", raising=False)

    command = "grep -n sudo README.md"
    transformed, sudo_stdin = terminal_tool_sudo._transform_sudo_command(command)

    assert transformed == command
    assert sudo_stdin is None


def test_actual_sudo_command_uses_configured_password(monkeypatch):
    monkeypatch.setenv("SUDO_PASSWORD", "testpass")
    monkeypatch.delenv("HERMES_INTERACTIVE", raising=False)

    transformed, sudo_stdin = terminal_tool_sudo._transform_sudo_command("sudo apt install -y ripgrep")

    assert transformed == "sudo -S -p '' apt install -y ripgrep"
    assert sudo_stdin == "testpass\n"


def test_actual_sudo_after_leading_env_assignment_is_rewritten(monkeypatch):
    monkeypatch.setenv("SUDO_PASSWORD", "testpass")
    monkeypatch.delenv("HERMES_INTERACTIVE", raising=False)

    transformed, sudo_stdin = terminal_tool_sudo._transform_sudo_command("DEBUG=1 sudo whoami")

    assert transformed == "DEBUG=1 sudo -S -p '' whoami"
    assert sudo_stdin == "testpass\n"


def test_explicit_empty_sudo_password_tries_empty_without_prompt(monkeypatch):
    monkeypatch.setenv("SUDO_PASSWORD", "")
    monkeypatch.setenv("HERMES_INTERACTIVE", "1")

    def _fail_prompt(*_args, **_kwargs):
        raise AssertionError("interactive sudo prompt should not run for explicit empty password")

    monkeypatch.setattr(terminal_tool_sudo, "_prompt_for_sudo_password", _fail_prompt)

    transformed, sudo_stdin = terminal_tool_sudo._transform_sudo_command("sudo true")

    assert transformed == "sudo -S -p '' true"
    assert sudo_stdin == "\n"


def test_cached_sudo_password_is_used_when_env_is_unset(monkeypatch):
    monkeypatch.delenv("SUDO_PASSWORD", raising=False)
    monkeypatch.delenv("HERMES_INTERACTIVE", raising=False)
    terminal_tool_sudo._set_cached_sudo_password("cached-pass")

    transformed, sudo_stdin = terminal_tool_sudo._transform_sudo_command("echo ok && sudo whoami")

    assert transformed == "echo ok && sudo -S -p '' whoami"
    assert sudo_stdin == "cached-pass\n"


def test_registered_sudo_callback_is_used_without_interactive_env(monkeypatch):
    monkeypatch.delenv("SUDO_PASSWORD", raising=False)
    monkeypatch.delenv("HERMES_INTERACTIVE", raising=False)

    calls = []

    def sudo_callback():
        calls.append("called")
        return "callback-pass"

    terminal_tool.set_sudo_password_callback(sudo_callback)
    try:
        transformed, sudo_stdin = terminal_tool_sudo._transform_sudo_command(
            "echo ok | sudo tee /tmp/hermes-test",
            sudo_nopasswd_check=lambda: False,
        )
    finally:
        terminal_tool.set_sudo_password_callback(None)

    assert calls == ["called"]
    assert transformed == "echo ok | sudo -S -p '' tee /tmp/hermes-test"
    assert sudo_stdin == "callback-pass\n"


def test_cached_sudo_password_isolated_by_session_key(monkeypatch):
    monkeypatch.delenv("SUDO_PASSWORD", raising=False)
    monkeypatch.delenv("HERMES_INTERACTIVE", raising=False)

    monkeypatch.setenv("HERMES_SESSION_KEY", "session-a")
    terminal_tool_sudo._set_cached_sudo_password("alpha-pass")

    monkeypatch.setenv("HERMES_SESSION_KEY", "session-b")
    assert terminal_tool_sudo._get_cached_sudo_password() == ""

    monkeypatch.setenv("HERMES_SESSION_KEY", "session-a")
    assert terminal_tool_sudo._get_cached_sudo_password() == "alpha-pass"


def test_passwordless_sudo_skips_interactive_prompt_and_rewrite(monkeypatch):
    monkeypatch.delenv("SUDO_PASSWORD", raising=False)
    monkeypatch.delenv("TERMINAL_ENV", raising=False)
    monkeypatch.setenv("HERMES_INTERACTIVE", "1")

    def _fail_prompt(*_args, **_kwargs):
        raise AssertionError(
            "interactive sudo prompt should not run when sudo -n already works"
        )

    monkeypatch.setattr(terminal_tool_sudo, "_prompt_for_sudo_password", _fail_prompt)

    transformed, sudo_stdin = terminal_tool_sudo._transform_sudo_command(
        "sudo whoami", sudo_nopasswd_check=lambda: True
    )

    assert transformed == "sudo whoami"
    assert sudo_stdin is None


def test_passwordless_sudo_probe_rechecks_local_terminal(monkeypatch):
    from tools.environments.local import LocalEnvironment
    env = LocalEnvironment()
    calls = []

    def fake_wait(proc, timeout=None):
        calls.append(proc)
        return {"returncode": 0 if len(calls) == 1 else 1}

    monkeypatch.setattr(env, "_run_bash", lambda cmd, **kw: cmd)
    monkeypatch.setattr(env, "_wait_for_process", fake_wait)

    assert env._sudo_nopasswd_works() is True
    assert env._sudo_nopasswd_works() is False
    assert len(calls) == 2


def test_passwordless_sudo_probe_is_disabled_for_nonlocal_terminal_env(monkeypatch):
    from tools.environments.base import BaseEnvironment

    class DummyEnv(BaseEnvironment):
        def cleanup(self):
            pass

    env = DummyEnv(cwd="/tmp", timeout=10)
    assert env._sudo_nopasswd_works() is False


def test_validate_workdir_allows_windows_drive_paths():
    assert terminal_tool_guards._validate_workdir(r"C:\Users\Alice\project") is None
    assert terminal_tool_guards._validate_workdir("C:/Users/Alice/project") is None


def test_validate_workdir_allows_windows_unc_paths():
    assert terminal_tool_guards._validate_workdir(r"\\server\share\project") is None

def test_headless_sudo_never_runs_backend_nopasswd_probe(monkeypatch):
    """No prompt can fire without a UI, so the backend round trip must not be paid."""
    monkeypatch.delenv("SUDO_PASSWORD", raising=False)
    monkeypatch.delenv("HERMES_INTERACTIVE", raising=False)
    terminal_tool.set_sudo_password_callback(None)

    def _fail_probe():
        raise AssertionError("headless sudo must not probe the backend")

    assert terminal_tool_sudo._transform_sudo_command("sudo true", sudo_nopasswd_check=_fail_probe) == (
        "sudo true", None)


def test_validate_workdir_blocks_shell_metacharacters_in_windows_paths():
    assert terminal_tool_guards._validate_workdir(r"C:\Users\Alice\project; rm -rf /")
    assert terminal_tool_guards._validate_workdir(r"C:\Users\Alice\project$(whoami)")
    assert terminal_tool_guards._validate_workdir("C:\\Users\\Alice\\project\nwhoami")



def test_get_env_config_ignores_bad_docker_json_for_local_backend(monkeypatch):
    """Docker-only JSON env vars must not break the default local backend."""
    monkeypatch.setenv("TERMINAL_ENV", "local")
    monkeypatch.setenv("TERMINAL_DOCKER_VOLUMES", "None")
    monkeypatch.setenv("TERMINAL_DOCKER_ENV", "not-json")
    monkeypatch.setenv("TERMINAL_DOCKER_FORWARD_ENV", "not-json")
    monkeypatch.setenv("TERMINAL_DOCKER_EXTRA_ARGS", "not-json")

    config = terminal_tool._get_env_config()

    assert config["env_type"] == "local"
    assert config["docker_volumes"] == []
    assert config["docker_env"] == {}
    assert config["docker_forward_env"] == []
    assert config["docker_extra_args"] == []


def test_get_env_config_ignores_bad_docker_json_for_ssh_backend(monkeypatch):
    """Non-container remote backends should also ignore Docker-only JSON."""
    monkeypatch.setenv("TERMINAL_ENV", "ssh")
    monkeypatch.setenv("TERMINAL_DOCKER_VOLUMES", "None")
    monkeypatch.setenv("TERMINAL_DOCKER_ENV", "not-json")

    config = terminal_tool._get_env_config()

    assert config["env_type"] == "ssh"
    assert config["docker_volumes"] == []
    assert config["docker_env"] == {}


def test_get_env_config_preserves_ssh_tilde_cwd(monkeypatch):
    """SSH cwd '~' is expanded by the remote shell, not the Hermes host."""
    monkeypatch.setenv("TERMINAL_ENV", "ssh")
    monkeypatch.setenv("TERMINAL_CWD", "~")
    monkeypatch.setenv("HOME", "/opt/data")

    config = terminal_tool._get_env_config()

    assert config["env_type"] == "ssh"
    assert config["cwd"] == "~"


def test_get_env_config_preserves_ssh_tilde_child_cwd(monkeypatch):
    """SSH cwd '~/x' must not become the local/container HOME path."""
    monkeypatch.setenv("TERMINAL_ENV", "ssh")
    monkeypatch.setenv("TERMINAL_CWD", "~/project")
    monkeypatch.setenv("HOME", "/opt/data")

    config = terminal_tool._get_env_config()

    assert config["env_type"] == "ssh"
    assert config["cwd"] == "~/project"


def test_get_env_config_still_rejects_bad_docker_json_for_docker_backend(monkeypatch):
    """Selecting Docker should keep the existing actionable config error."""
    monkeypatch.setenv("TERMINAL_ENV", "docker")
    monkeypatch.setenv("TERMINAL_DOCKER_VOLUMES", "None")

    try:
        terminal_tool._get_env_config()
    except ValueError as exc:
        assert "TERMINAL_DOCKER_VOLUMES" in str(exc)
    else:
        raise AssertionError("Docker backend must validate TERMINAL_DOCKER_VOLUMES")


def test_nano_without_pty_rejected(monkeypatch):
    """Interactive editors without PTY should be rejected immediately."""
    monkeypatch.setenv("TERMINAL_ENV", "local")
    result = terminal_tool.terminal_tool("nano config.txt")
    data = json.loads(result)
    assert data["exit_code"] == -1
    assert "pty=true" in data["error"]


def test_vim_without_pty_rejected(monkeypatch):
    """vim without PTY should be rejected."""
    monkeypatch.setenv("TERMINAL_ENV", "local")
    result = terminal_tool.terminal_tool("vim config.txt")
    data = json.loads(result)
    assert data["exit_code"] == -1


def test_editor_with_pty_allowed():
    """Interactive editors with PTY should be allowed (regex doesn't fire when pty=True)."""
    from tools.terminal_tool import _INTERACTIVE_EDITOR_RE
    assert _INTERACTIVE_EDITOR_RE.search("nano config.txt")
    assert _INTERACTIVE_EDITOR_RE.search("vim config.txt")


def test_editor_name_as_argument_substring_not_rejected():
    """An editor NAME inside an argument (a branch name, a message) must NOT
    trigger the guard — only an editor in COMMAND position does. Regression:
    `git push origin evolution/issue-215-execute-code-diagnostics` matched
    `\\bcode\\b` and was wrongly rejected, silently blocking the pipeline's pushes."""
    from tools.terminal_tool import _INTERACTIVE_EDITOR_RE

    assert not _INTERACTIVE_EDITOR_RE.search(
        "git push origin evolution/issue-215-execute-code-diagnostics"
    )
    assert not _INTERACTIVE_EDITOR_RE.search(
        "gh pr create --head evolution/issue-215-execute-code-diagnostics --title x"
    )
    # genuine command-position editor invocations are still caught
    assert _INTERACTIVE_EDITOR_RE.search("echo done && vim notes.txt")
    assert _INTERACTIVE_EDITOR_RE.search("code .")
    assert _INTERACTIVE_EDITOR_RE.search("sudo nano /etc/hosts")


def test_non_editor_command_unchanged():
    """Non-editor commands should not be affected by the editor guard."""
    from tools.terminal_tool import _INTERACTIVE_EDITOR_RE, _strip_quotes
    assert not _INTERACTIVE_EDITOR_RE.search(_strip_quotes("ls -la"))
    assert not _INTERACTIVE_EDITOR_RE.search(_strip_quotes("cat file.txt"))
    assert not _INTERACTIVE_EDITOR_RE.search(_strip_quotes("git commit -m 'nano fix'"))


def test_editor_in_quotes_not_flagged():
    """Editor names inside quoted strings should not trigger the guard."""
    from tools.terminal_tool import _INTERACTIVE_EDITOR_RE, _strip_quotes
    cmd = 'git commit -m "use nano for editing"'
    stripped = _strip_quotes(cmd)
    assert not _INTERACTIVE_EDITOR_RE.search(stripped)


def test_sudo_wrong_password_failure_detects_rejection_output():
    output = (
        "sudo: Authentication failed, try again.\n\n"
        "sudo: maximum 3 incorrect authentication attempts\n"
    )
    assert terminal_tool_sudo._sudo_wrong_password_failure(output) is True


def test_sudo_wrong_password_failure_ignores_tty_required_message():
    output = "sudo: a terminal is required to authenticate"
    assert terminal_tool_sudo._sudo_wrong_password_failure(output) is False


def test_invalidate_cached_sudo_on_auth_failure_clears_session_cache(monkeypatch):
    monkeypatch.delenv("SUDO_PASSWORD", raising=False)
    terminal_tool_sudo._set_cached_sudo_password("wrong-pass")

    cleared = terminal_tool_sudo._invalidate_cached_sudo_on_auth_failure(
        "sudo apt install fprintd",
        "sudo: Authentication failed, try again.",
    )

    assert cleared is True
    assert terminal_tool_sudo._get_cached_sudo_password() == ""


def test_invalidate_cached_sudo_on_auth_failure_keeps_env_password(monkeypatch):
    monkeypatch.setenv("SUDO_PASSWORD", "from-env")
    terminal_tool_sudo._set_cached_sudo_password("wrong-pass")

    cleared = terminal_tool_sudo._invalidate_cached_sudo_on_auth_failure(
        "sudo true",
        "sudo: Authentication failed, try again.",
    )

    assert cleared is False
    assert terminal_tool_sudo._get_cached_sudo_password() == "wrong-pass"


def test_transform_sudo_command_pipes_one_password_line_per_invocation(monkeypatch):
    monkeypatch.setenv("SUDO_PASSWORD", "testpass")
    monkeypatch.delenv("HERMES_INTERACTIVE", raising=False)

    transformed, sudo_stdin = terminal_tool_sudo._transform_sudo_command(
        "sudo true && sudo whoami"
    )

    assert transformed == "sudo -S -p '' true && sudo -S -p '' whoami"
    assert sudo_stdin == "testpass\ntestpass\n"


def test_validate_workdir_allows_unicode_filesystem_paths():
    assert terminal_tool_guards._validate_workdir(
        "/Users/alice/Documents/Obs_Hermes_Data/项目-projects/客户拜访"
    ) is None
    assert terminal_tool_guards._validate_workdir("/tmp/テスト") is None
    assert terminal_tool_guards._validate_workdir("/home/jürgen/über projekt") is None


def test_validate_workdir_still_blocks_metachars_in_unicode_paths():
    # Widening to Unicode letters must not open the injection boundary:
    # shell metacharacters and control chars stay rejected even when mixed
    # with non-ASCII path segments.
    assert terminal_tool_guards._validate_workdir("/tmp/テスト; rm -rf /")
    assert terminal_tool_guards._validate_workdir("/tmp/项目$(whoami)")
    assert terminal_tool_guards._validate_workdir("/tmp/über`id`")
    assert terminal_tool_guards._validate_workdir("/tmp/テスト\nwhoami")
    assert terminal_tool_guards._validate_workdir("/tmp/项目|cat /etc/passwd")
    assert terminal_tool_guards._validate_workdir("/tmp/ü\x00ber")


def test_literal_sudo_executables_receive_password_stdin(monkeypatch):
    monkeypatch.setenv("SUDO_PASSWORD", "testpass")
    for prefix in ("", "VAR='a b' ", "env ", "'/usr/bin/env' -i -u UNUSED X=1 ",
                   "env --unset=UNUSED --chdir /tmp -- X=1 ", "env -uUNUSED -C/tmp "):
        for executable in ("sudo", "/usr/bin/sudo", "'/opt/my tools/sudo'", '"/usr/bin/sudo"'):
            command = prefix + executable + " -u root true"
            rewritten, stdin = terminal_tool_sudo._transform_sudo_command(command)
            assert rewritten == prefix + executable + " -S -p '' -u root true"
            assert stdin == "testpass\n"


def test_sudo_rewrite_preserves_env_operands_and_prose(monkeypatch):
    monkeypatch.setenv("SUDO_PASSWORD", "testpass")
    commands = (
        "echo '/usr/bin/sudo true'", "env echo sudo true", "env -u sudo echo ok",
        "env --chdir sudo echo ok", "env --unset=sudo echo ok", "env -- sudo=1 echo ok",
        ">/tmp/sudo echo ok", "env 2>/tmp/sudo echo ok", "env > /tmp/sudo echo ok",
        "/tmp/{a,b}/sudo true", "env X=1 -u UNUSED sudo", "env - -u UNUSED sudo", "env echo /usr/bin/sudo", "/tmp/*/sudo true",
        "env -S 'sudo true'", "env --unknown sudo true", "env --help sudo",
        "bash -c 'sudo true'", "echo ok # prose; /usr/bin/sudo true",
        '"/usr/bin/sudo', "env -u sudo", "env X=sudo", '"X=1" /usr/bin/sudo true',
    )
    for command in commands:
        assert terminal_tool_sudo._transform_sudo_command(command) == (command, None)


def test_count_real_sudo_invocations_ignores_mentions(monkeypatch):
    assert terminal_tool_sudo._count_real_sudo_invocations("grep sudo README.md") == 0
    assert terminal_tool_sudo._count_real_sudo_invocations("sudo a; sudo b") == 2
