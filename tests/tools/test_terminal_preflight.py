"""Tests for the terminal preflight guard (#3243)."""

import json

import pytest

from tools.terminal_tool import _first_command_token, _preflight_check, terminal_tool


@pytest.mark.parametrize(
    "command,expected",
    [
        ("ls -la /tmp", "ls"),
        ("'ls' -la", "ls"),
        ("cd /tmp && ls", None),
        ("cat file | grep x", None),
        ("echo hi > file", None),
        ("echo $(date)", None),
        ("FOO=bar python script.py", None),
    ],
)
def test_first_command_token(command, expected):
    assert _first_command_token(command) == expected


@pytest.mark.parametrize(
    "command,workdir,timeout,should_fail",
    [
        ("ls", "/nonexistent_dir_3243", None, True),
        ("definitely_not_a_command_3243", None, None, True),
        ("ls", None, 0, True),
        ("cd /tmp", None, None, False),
        ("cd /tmp && ls", None, None, False),
    ],
)
def test_preflight_check(command, workdir, timeout, should_fail):
    assert (_preflight_check(command, workdir, timeout) is not None) == should_fail


def test_terminal_tool_preflight_missing_bin():
    result = json.loads(
        terminal_tool("definitely_not_a_command_3243", task_id="t-preflight-1")
    )
    assert result["exit_code"] == -1
    assert "was not found in PATH" in result["error"]


def test_terminal_tool_preflight_missing_workdir(tmp_path):
    result = json.loads(
        terminal_tool("ls", workdir=str(tmp_path / "missing"), task_id="t-preflight-2")
    )
    assert result["exit_code"] == -1
    assert "does not exist or is not a directory" in result["error"]


def test_terminal_tool_preflight_existing_workdir_ok(tmp_path):
    result = json.loads(
        terminal_tool("pwd", workdir=str(tmp_path), task_id="t-preflight-3")
    )
    assert result["exit_code"] == 0
