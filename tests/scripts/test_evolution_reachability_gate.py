# -*- coding: utf-8 -*-
"""Tests for scripts/evolution_reachability_gate.py — deterministic dead-code reachability gate (#2498)."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from evolution_reachability_gate import (  # noqa: E402
    _is_source_python_file,
    _is_test_file,
    check_reachability,
    extract_top_level_symbols,
    find_symbol_references,
)


def _f(path: str, content: str = "") -> dict:
    return {"path": path, "content": content}


class TestFilePredicates:
    def test_is_test_file(self):
        assert _is_test_file("tests/scripts/test_merge.py")
        assert _is_test_file("tests/agent/test_compress.py")
        assert _is_test_file("agent/tests/test_x.py")
        assert _is_test_file("test_foo.py")
        assert _is_test_file("utils/helper_test.py")

        assert not _is_test_file("agent/context_compressor.py")
        assert not _is_test_file("evolution/lib/parallel_compaction.py")
        assert not _is_test_file("scripts/evolution_reachability_gate.py")

    def test_is_source_python_file(self):
        assert _is_source_python_file("agent/context_compressor.py")
        assert _is_source_python_file("evolution/lib/parallel_compaction.py")

        assert not _is_source_python_file("tests/test_foo.py")
        assert not _is_source_python_file("README.md")
        assert not _is_source_python_file("config.yaml")


class TestExtractTopLevelSymbols:
    def test_extracts_classes_and_functions(self):
        code = """
class ParallelCompactor:
    def inner_method(self):
        pass

def write_snapshot():
    pass

async def async_fetch():
    pass

def _private_func():
    pass

class _PrivateClass:
    pass

def main():
    pass
"""
        symbols = extract_top_level_symbols(code)
        assert "ParallelCompactor" in symbols
        assert "write_snapshot" in symbols
        assert "async_fetch" in symbols
        assert "inner_method" not in symbols
        assert "_private_func" not in symbols
        assert "_PrivateClass" not in symbols
        assert "main" not in symbols

    def test_handles_syntax_error(self):
        code = "def incomplete_func("
        assert extract_top_level_symbols(code) == []


class TestFindSymbolReferences:
    def test_finds_production_references_only(self):
        all_files = {
            "evolution/lib/compactor.py": "class ParallelCompactor:\n    pass\n",
            "agent/context_compressor.py": "from evolution.lib.compactor import ParallelCompactor\n",
            "tests/test_compactor.py": "from evolution.lib.compactor import ParallelCompactor\n",
        }
        refs = find_symbol_references(
            symbol="ParallelCompactor",
            defining_file="evolution/lib/compactor.py",
            all_files=all_files,
        )
        assert refs == ["agent/context_compressor.py"]

    def test_no_references_outside_defining_and_tests(self):
        all_files = {
            "evolution/lib/unwired.py": "class UnwiredSymbol:\n    pass\n",
            "tests/test_unwired.py": "from evolution.lib.unwired import UnwiredSymbol\n",
        }
        refs = find_symbol_references(
            symbol="UnwiredSymbol",
            defining_file="evolution/lib/unwired.py",
            all_files=all_files,
        )
        assert refs == []


class TestCheckReachability:
    def test_unreachable_symbol_generates_violation(self):
        files = [
            _f(
                "evolution/lib/dead.py",
                content="class DeadCodeClass:\n    pass\n\ndef dead_func():\n    pass\n",
            )
        ]
        source_contents = {
            "evolution/lib/dead.py": "class DeadCodeClass:\n    pass\n\ndef dead_func():\n    pass\n",
            "tests/test_dead.py": "from evolution.lib.dead import DeadCodeClass, dead_func\n",
        }
        violations = check_reachability(files, source_contents=source_contents)
        assert len(violations) == 2
        assert any("DeadCodeClass" in v for v in violations)
        assert any("dead_func" in v for v in violations)
        assert all("DEAD_CODE_UNREACHABLE" in v for v in violations)

    def test_reachable_symbol_clears_gate(self):
        files = [
            _f(
                "evolution/lib/active.py",
                content="class ActiveClass:\n    pass\n",
            )
        ]
        source_contents = {
            "evolution/lib/active.py": "class ActiveClass:\n    pass\n",
            "agent/caller.py": "from evolution.lib.active import ActiveClass\n",
            "tests/test_active.py": "from evolution.lib.active import ActiveClass\n",
        }
        violations = check_reachability(files, source_contents=source_contents)
        assert violations == []

    def test_skips_non_python_and_test_files(self):
        files = [
            _f("README.md", content="# Docs"),
            _f("tests/test_foo.py", content="def test_something(): assert True"),
        ]
        assert check_reachability(files, source_contents={}) == []
