"""Tests for the CFG/DFG extractor module (issue #1221)."""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from scripts.evolution_cfg_dfg_extractor import (
    extract_cfg_dfg,
    extract_from_file,
    main,
)


SAMPLE_SOURCE = """
def process_data(data, threshold):
    result = []
    for item in data:
        if item > threshold:
            result.append(item * 2)
        else:
            result.append(item)
    return result

def helper(x):
    y = x + 1
    return y

class MyClass:
    def method(self, value):
        if value > 0:
            self.value = value
            self.helper(value)
        return self.value
"""


class TestExtractCfgDfg:
    """Test the extract_cfg_dfg function."""

    def test_extracts_functions(self):
        result = extract_cfg_dfg(SAMPLE_SOURCE, "test.py")
        assert result["file"] == "test.py"
        func_names = [f["name"] for f in result["functions"]]
        assert "process_data" in func_names
        assert "helper" in func_names
        assert "method" in func_names

    def test_extracts_branches(self):
        result = extract_cfg_dfg(SAMPLE_SOURCE, "test.py")
        process_data = next(
            f for f in result["functions"] if f["name"] == "process_data"
        )
        branch_types = [b["type"] for b in process_data["branches"]]
        assert "For" in branch_types
        assert "If" in branch_types

    def test_extracts_calls(self):
        result = extract_cfg_dfg(SAMPLE_SOURCE, "test.py")
        process_data = next(
            f for f in result["functions"] if f["name"] == "process_data"
        )
        assert "result.append" in process_data["calls"]

    def test_extracts_assignments(self):
        result = extract_cfg_dfg(SAMPLE_SOURCE, "test.py")
        process_data = next(
            f for f in result["functions"] if f["name"] == "process_data"
        )
        targets = [a["target"] for a in process_data["assignments"]]
        assert "result" in targets

    def test_extracts_returns(self):
        result = extract_cfg_dfg(SAMPLE_SOURCE, "test.py")
        helper = next(f for f in result["functions"] if f["name"] == "helper")
        assert len(helper["returns"]) == 1

    def test_syntax_error_handled(self):
        result = extract_cfg_dfg("def broken(:", "bad.py")
        assert "error" in result
        assert result["functions"] == []

    def test_empty_source(self):
        result = extract_cfg_dfg("", "empty.py")
        assert result["functions"] == []

    def test_attribute_assignment(self):
        result = extract_cfg_dfg(SAMPLE_SOURCE, "test.py")
        method = next(f for f in result["functions"] if f["name"] == "method")
        targets = [a["target"] for a in method["assignments"]]
        assert "self.value" in targets


class TestExtractFromFile:
    """Test the extract_from_file function."""

    def test_reads_file(self, tmp_path):
        f = tmp_path / "test.py"
        f.write_text(SAMPLE_SOURCE, encoding="utf-8")
        result = extract_from_file(str(f))
        assert result["file"] == str(f)
        assert len(result["functions"]) >= 2

    def test_nonexistent_file(self):
        result = extract_from_file("/nonexistent/path.py")
        assert "error" in result


class TestCLI:
    """Test the CLI entry point."""

    def test_cli_file_output(self, tmp_path, capsys):
        f = tmp_path / "test.py"
        f.write_text(SAMPLE_SOURCE, encoding="utf-8")
        rc = main(["--file", str(f)])
        assert rc == 0
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert "functions" in data

    def test_cli_file_output_to_file(self, tmp_path):
        src = tmp_path / "test.py"
        src.write_text(SAMPLE_SOURCE, encoding="utf-8")
        out = tmp_path / "output.json"
        rc = main(["--file", str(src), "--output", str(out)])
        assert rc == 0
        assert out.exists()
        data = json.loads(out.read_text(encoding="utf-8"))
        assert "functions" in data

    def test_cli_no_args_prints_help(self, capsys):
        rc = main([])
        assert rc == 1
