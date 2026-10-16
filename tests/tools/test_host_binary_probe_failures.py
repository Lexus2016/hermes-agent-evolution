"""Host binary overwrite checks must distinguish missing files from failed stat."""

import errno
import os
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from tests.tools.test_remote_binary_write_guard import _OPERATIONS, _binary_payload, _write_file
from tools import file_tools, terminal_tool
from tools import file_tools_write_guards as guards
from tools.environments.local import LocalEnvironment


@pytest.fixture
def local_target(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    task = "host-probe-" + uuid.uuid4().hex
    env = LocalEnvironment(cwd=str(tmp_path))
    terminal_tool.register_task_env_overrides(task, {"env_type": "local", "cwd": str(tmp_path)})
    with terminal_tool._env_lock:
        terminal_tool._active_environments[task] = env
    try:
        assert file_tools._file_ops_uses_host_paths(file_tools._get_file_ops(task))
        yield SimpleNamespace(root=tmp_path, task=task)
    finally:
        file_tools.clear_file_ops_cache(task)
        with terminal_tool._env_lock:
            terminal_tool._active_environments.pop(task, None)
        terminal_tool._task_env_overrides.pop(task, None)
        terminal_tool._creation_locks.pop(task, None)
        terminal_tool._last_activity.pop(task, None)


@pytest.mark.parametrize("operation", sorted(_OPERATIONS))
@pytest.mark.parametrize("error_number", [errno.EACCES, errno.EIO, None])
def test_transient_stat_failure_refuses_binary_mutation(local_target, operation, error_number):
    target = local_target.root / "existing.pdf"
    original = _binary_payload(".pdf")
    target.write_bytes(original)
    real_stat = os.stat
    faults = []

    def transient_stat(path, *args, **kwargs):
        if os.fspath(path) == str(target) and not faults:
            faults.append(path)
            raise OSError(error_number, "synthetic transient stat failure")
        return real_stat(path, *args, **kwargs)

    with patch("os.stat", transient_stat):
        result = _OPERATIONS[operation](target, local_target.task)
    assert len(faults) == 1
    assert "establish" in result.get("error", ""), result
    assert target.read_bytes() == original


@pytest.mark.parametrize("operation", sorted(_OPERATIONS))
def test_existing_binary_control_is_refused(local_target, operation):
    target = local_target.root / "existing.pdf"
    original = _binary_payload(".pdf")
    target.write_bytes(original)
    result = _OPERATIONS[operation](target, local_target.task)
    assert "Refusing to overwrite existing PDF" in result["error"]
    assert target.read_bytes() == original


@pytest.mark.parametrize("suffix", [".pdf", ".txt"])
def test_confirmed_missing_file_remains_writable(local_target, suffix):
    target = local_target.root / ("new" + suffix)
    assert guards._target_regular_file_state(str(target), local_target.task) == "absent"
    result = _write_file(target, local_target.task)
    assert not result.get("error"), result
    assert target.read_text(encoding="utf-8-sig") == "plain text replacement"


@pytest.mark.parametrize("operation", ["patch_replace", "patch_v4a_update"])
def test_regular_text_patch_remains_usable(local_target, operation):
    target = local_target.root / "normal.txt"
    target.write_text("KEEPME line\n", encoding="utf-8")
    result = _OPERATIONS[operation](target, local_target.task)
    assert result.get("success"), result
    assert target.read_text(encoding="utf-8-sig") == "CHANGED line\n"


@pytest.mark.require_symlinks
def test_host_probe_follows_symlinks_and_classifies_dangling_target(local_target):
    target = local_target.root / "existing.pdf"
    target.write_bytes(_binary_payload(".pdf"))
    link = local_target.root / "link.pdf"
    link.symlink_to(target)
    assert guards._target_regular_file_state(str(link), local_target.task) == "exists"
    target.unlink()
    assert guards._target_regular_file_state(str(link), local_target.task) == "absent"
    assert link.is_symlink()


def test_directory_target_remains_refused(local_target):
    target = local_target.root / "directory.pdf"
    target.mkdir()
    result = _write_file(target, local_target.task)
    assert result.get("error"), result
    assert target.is_dir()


@pytest.mark.platforms("posix")
def test_fifo_target_remains_refused(local_target):
    target = local_target.root / "pipe.pdf"
    os.mkfifo(target)
    result = _write_file(target, local_target.task)
    assert result.get("error"), result
    assert target.is_fifo()
