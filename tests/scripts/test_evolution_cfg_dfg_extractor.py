"""Tests for the control-flow/data-flow graph extractor (#1221)."""

import json
import textwrap
from pathlib import Path

import pytest

from scripts.evolution_cfg_dfg_extractor import (
    extract_function_graph,
    extract_graphs,
    extract_graphs_from_diff,
    extract_graphs_from_file,
    graphs_to_json,
)


@pytest.fixture
def sample_source():
    """A small Python source with branches, calls, assignments, returns."""
    return textwrap.dedent("""\
        def compute(x, y):
            result = x + y
            if result > 0:
                print(result)
                return result
            else:
                log_error("negative")
                return None

        def helper(val):
            data = process(val, mode="strict")
            assert data is not None
            return data
        """)


@pytest.fixture
def sample_file(tmp_path, sample_source):
    f = tmp_path / "sample.py"
    f.write_text(sample_source)
    return f


class TestExtractGraphs:
    """extract_graphs from source string."""

    def test_extracts_all_functions(self, sample_source):
        graphs = extract_graphs(sample_source)
        assert len(graphs) == 2
        names = {g["function"] for g in graphs}
        assert names == {"compute", "helper"}

    def test_syntax_error_returns_empty(self):
        graphs = extract_graphs("def broken(:\n  pass")
        assert graphs == []

    def test_empty_source_returns_empty(self):
        assert extract_graphs("") == []

    def test_no_functions_returns_empty(self):
        assert extract_graphs("x = 1\ny = 2\n") == []


class TestControlFlow:
    """Control-flow entries: branches, calls, returns."""

    def test_if_branch_detected(self, sample_source):
        graphs = extract_graphs(sample_source)
        compute = next(g for g in graphs if g["function"] == "compute")
        branches = compute["control_flow"]["branches"]
        assert any(b["type"] == "if" for b in branches)

    def test_assert_branch_detected(self, sample_source):
        graphs = extract_graphs(sample_source)
        helper = next(g for g in graphs if g["function"] == "helper")
        branches = helper["control_flow"]["branches"]
        assert any(b["type"] == "assert" for b in branches)

    def test_calls_detected(self, sample_source):
        graphs = extract_graphs(sample_source)
        compute = next(g for g in graphs if g["function"] == "compute")
        calls = compute["control_flow"]["calls"]
        call_names = [c["name"] for c in calls]
        assert "print" in call_names
        assert "log_error" in call_names

    def test_returns_detected(self, sample_source):
        graphs = extract_graphs(sample_source)
        compute = next(g for g in graphs if g["function"] == "compute")
        returns = compute["control_flow"]["returns"]
        assert len(returns) == 2

    def test_for_loop_detected(self):
        source = "def loop_func(items):\n    for item in items:\n        print(item)\n"
        graphs = extract_graphs(source)
        branches = graphs[0]["control_flow"]["branches"]
        assert any(b["type"] == "for" for b in branches)

    def test_while_loop_detected(self):
        source = "def w():\n    while True:\n        break\n"
        graphs = extract_graphs(source)
        branches = graphs[0]["control_flow"]["branches"]
        assert any(b["type"] == "while" for b in branches)

    def test_try_block_detected(self):
        source = (
            "def t():\n    try:\n        x = 1\n    except Exception:\n        pass\n"
        )
        graphs = extract_graphs(source)
        branches = graphs[0]["control_flow"]["branches"]
        assert any(b["type"] == "try" for b in branches)


class TestDataFlow:
    """Data-flow entries: assignments, arg_passing."""

    def test_assignment_detected(self, sample_source):
        graphs = extract_graphs(sample_source)
        compute = next(g for g in graphs if g["function"] == "compute")
        assignments = compute["data_flow"]["assignments"]
        targets = [a["target"] for a in assignments]
        assert "result" in targets

    def test_arg_passing_detected(self, sample_source):
        graphs = extract_graphs(sample_source)
        helper = next(g for g in graphs if g["function"] == "helper")
        arg_passing = helper["data_flow"]["arg_passing"]
        assert any(ap["func"] == "process" for ap in arg_passing)
        assert any(ap["arg"] == "val" for ap in arg_passing)

    def test_method_call_name(self):
        source = "def m(obj):\n    obj.do_thing(x)\n"
        graphs = extract_graphs(source)
        calls = graphs[0]["control_flow"]["calls"]
        assert any(c["name"] == "obj.do_thing" for c in calls)


class TestExtractFromFile:
    """extract_graphs_from_file reads from disk and adds file field."""

    def test_file_extraction(self, sample_file):
        graphs = extract_graphs_from_file(sample_file)
        assert len(graphs) == 2
        assert all("file" in g for g in graphs)

    def test_nonexistent_file_returns_empty(self, tmp_path):
        graphs = extract_graphs_from_file(tmp_path / "nope.py")
        assert graphs == []


class TestExtractFromDiff:
    """extract_graphs_from_diff parses unified diff and extracts from changed .py files."""

    def test_diff_extraction(self, tmp_path, sample_source):
        # Create the file the diff references
        (tmp_path / "sample.py").write_text(sample_source)

        diff = textwrap.dedent("""\
            diff --git a/sample.py b/sample.py
            index 1234567..abcdefg 100644
            --- a/sample.py
            +++ b/sample.py
            @@ -1,3 +1,5 @@
            +def compute(x, y):
            +    result = x + y
            +    return result
            """)
        result = extract_graphs_from_diff(diff, repo_root=tmp_path)
        assert "sample.py" in result
        assert len(result["sample.py"]) == 2  # compute + helper

    def test_diff_skips_non_python_files(self, tmp_path):
        (tmp_path / "README.md").write_text("# hello")
        diff = "diff --git a/README.md b/README.md\n--- a/README.md\n+++ b/README.md\n"
        result = extract_graphs_from_diff(diff, repo_root=tmp_path)
        assert result == {}

    def test_diff_missing_file_skipped(self, tmp_path):
        diff = "diff --git a/gone.py b/gone.py\n--- a/gone.py\n+++ b/gone.py\n"
        result = extract_graphs_from_diff(diff, repo_root=tmp_path)
        assert result == {}


class TestGraphsToJson:
    """graphs_to_json serializes both list and dict inputs."""

    def test_serialize_list(self, sample_source):
        graphs = extract_graphs(sample_source)
        j = graphs_to_json(graphs)
        parsed = json.loads(j)
        assert isinstance(parsed, list)
        assert len(parsed) == 2

    def test_serialize_dict(self, sample_file):
        from scripts.evolution_cfg_dfg_extractor import extract_graphs_from_file

        graphs = {"sample.py": extract_graphs_from_file(sample_file)}
        j = graphs_to_json(graphs)
        parsed = json.loads(j)
        assert isinstance(parsed, dict)
        assert "sample.py" in parsed
