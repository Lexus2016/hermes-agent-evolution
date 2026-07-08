"""Tests for read_file batch mode (multi-path reads).

#757/#784 — read_file accepts a list of paths to read multiple files
in a single tool call, returning per-file results in a structured JSON.
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tools.file_tools import _handle_read_file, _BATCH_READ_MAX_FILES


@pytest.fixture
def _two_files(tmp_path):
    f1 = tmp_path / "alpha.txt"
    f1.write_text("alpha\nbravo\ncharlie\n")
    f2 = tmp_path / "beta.txt"
    f2.write_text("delta\necho\nfoxtrot\n")
    return str(f1), str(f2)


def test_batch_read_returns_per_file_content(_two_files):
    f1, f2 = _two_files
    result = _handle_read_file({"path": [f1, f2]}, task_id="test-batch")
    data = json.loads(result)
    assert data["batch"] is True
    assert len(data["files"]) == 2
    assert data["files"][0]["path"] == f1
    assert "alpha" in data["files"][0]["content"]
    assert data["files"][1]["path"] == f2
    assert "delta" in data["files"][1]["content"]


def test_batch_read_preserves_total_lines(_two_files):
    f1, f2 = _two_files
    result = _handle_read_file({"path": [f1, f2]}, task_id="test-batch")
    data = json.loads(result)
    assert data["files"][0]["total_lines"] == 3
    assert data["files"][1]["total_lines"] == 3


def test_batch_read_with_nonexistent_file_includes_error(tmp_path):
    good = tmp_path / "exists.txt"
    good.write_text("hello\n")
    ghost = str(tmp_path / "ghost.txt")
    result = _handle_read_file({"path": [str(good), ghost]}, task_id="test-batch")
    data = json.loads(result)
    assert data["batch"] is True
    assert len(data["files"]) == 2
    assert "content" in data["files"][0]
    assert "hello" in data["files"][0]["content"]
    assert "error" in data["files"][1]
    assert data["files"][1]["path"] == ghost


def test_batch_read_respects_pagination(tmp_path):
    f = tmp_path / "numbered.txt"
    f.write_text("\n".join(f"LINE_{i:04d}" for i in range(1, 51)) + "\n")
    result = _handle_read_file({"path": [str(f)], "offset": 10, "limit": 5}, task_id="test-batch")
    data = json.loads(result)
    assert data["files"][0]["total_lines"] == 50
    assert "LINE_0010" in data["files"][0]["content"]
    assert "LINE_0014" in data["files"][0]["content"]
    assert "LINE_0009" not in data["files"][0]["content"]


def test_batch_read_rejects_too_many_paths(tmp_path):
    paths = [str(tmp_path / f"file_{i}.txt") for i in range(_BATCH_READ_MAX_FILES + 1)]
    result = _handle_read_file({"path": paths}, task_id="test-batch")
    data = json.loads(result)
    assert "error" in data
    assert str(_BATCH_READ_MAX_FILES) in data["error"]


def test_single_string_path_still_works(tmp_path):
    """Backward compatibility: a single string path must not trigger batch mode."""
    f = tmp_path / "single.txt"
    f.write_text("solo\n")
    result = _handle_read_file({"path": str(f)}, task_id="test-single")
    data = json.loads(result)
    assert "batch" not in data
    assert "content" in data
    assert "solo" in data["content"]


def test_batch_read_empty_list_returns_empty_files():
    result = _handle_read_file({"path": []}, task_id="test-batch")
    data = json.loads(result)
    assert data["batch"] is True
    assert data["files"] == []