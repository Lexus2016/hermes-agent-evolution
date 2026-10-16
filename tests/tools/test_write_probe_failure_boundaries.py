"""Backend probe failures preserve binary bytes through actual tool dispatch."""

from unittest.mock import patch

import pytest

from tests.tools.test_remote_binary_write_guard import (
    _OPERATIONS,
    _binary_payload,
    remote_target,
)
from tools import file_tools_write_guards as guards

__all__ = ["remote_target"]  # Explicit pytest fixture export for this module.


@pytest.mark.parametrize("operation", sorted(_OPERATIONS))
@pytest.mark.parametrize("failure", ["resolve", "acquire", "probe"])
def test_probe_failure_refuses_existing_binary(remote_target, operation, failure):
    target = remote_target.target / "data.sqlite"
    original = _binary_payload(".sqlite")
    target.write_bytes(original)
    path = remote_target.view / "data.sqlite"
    task = remote_target.task_id
    if failure == "resolve":
        failing_call = "tools.file_tools_write_guards._resolve_path_for_task"
    elif failure == "acquire":
        failing_call = "tools.file_tools._get_file_ops"
    else:
        failing_call = "tools.file_tools.ShellFileOperations._probe_regular_file"
    with patch(failing_call, side_effect=RuntimeError("synthetic probe failure")):
        state = guards._target_regular_file_state(str(path), task)
        assert state == ("exists" if failure == "resolve" else "unavailable")
        result = _OPERATIONS[operation](path, task)
    assert result.get("error"), result
    if failure != "resolve":
        assert "Refusing" in result["error"]
    assert target.read_bytes() == original
    assert not path.exists()


def test_resolution_failure_still_probes_backend_absence(remote_target):
    """Resolution is optional: the backend expands the raw path before probing."""
    path = remote_target.view / "fresh.pdf"
    with patch("tools.file_tools_write_guards._resolve_path_for_task",
               side_effect=RuntimeError("synthetic resolution failure")):
        assert guards._target_regular_file_state(str(path), remote_target.task_id) == "absent"
        assert guards._check_binary_document_write(str(path), remote_target.task_id) is None
    assert not (remote_target.target / "fresh.pdf").exists()
    assert not path.exists()
