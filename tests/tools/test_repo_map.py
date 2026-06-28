"""Tests for the repo_map tool (stdlib ``ast`` Python symbol map).

Covers the Python-only minimal slice of issue #320:
  - extracts functions / classes / methods with file:line locations
  - reference-count rank orders hot symbols first
  - context-budget truncation caps output
  - a syntax-error file is skipped, not fatal
  - non-Python files are ignored
  - the tool is registered the same way the other file tools are

No tree-sitter, no new deps — this is the first additive slice; multi-language
tree-sitter ranking remains the follow-up epic.
"""

import json

from tools.repo_map import (
    extract_symbols,
    build_repo_map,
    _handle_repo_map,
    REPO_MAP_SCHEMA,
)


def _write(root, rel, content):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# Symbol extraction
# ---------------------------------------------------------------------------
class TestExtractSymbols:
    def test_extracts_functions_classes_methods_with_locations(self, tmp_path):
        src = (
            "def top_level_fn():\n"
            "    pass\n"
            "\n"
            "class Widget:\n"
            "    def method_a(self):\n"
            "        pass\n"
            "\n"
            "    def method_b(self):\n"
            "        pass\n"
        )
        f = _write(tmp_path, "mod.py", src)
        syms = extract_symbols(f)

        by_name = {s["name"]: s for s in syms}
        assert "top_level_fn" in by_name
        assert by_name["top_level_fn"]["kind"] == "function"
        assert by_name["top_level_fn"]["line"] == 1

        assert "Widget" in by_name
        assert by_name["Widget"]["kind"] == "class"
        assert by_name["Widget"]["line"] == 4

        assert "Widget.method_a" in by_name
        assert by_name["Widget.method_a"]["kind"] == "method"
        assert by_name["Widget.method_a"]["line"] == 5

        assert "Widget.method_b" in by_name
        assert by_name["Widget.method_b"]["line"] == 8

    def test_async_function_is_a_function(self, tmp_path):
        f = _write(tmp_path, "a.py", "async def fetch():\n    pass\n")
        syms = extract_symbols(f)
        kinds = {s["name"]: s["kind"] for s in syms}
        assert kinds["fetch"] == "function"

    def test_syntax_error_file_returns_empty_not_fatal(self, tmp_path):
        f = _write(tmp_path, "broken.py", "def oops(:\n  pass\n")
        # Must not raise — a broken file is skipped, returning no symbols.
        assert extract_symbols(f) == []


# ---------------------------------------------------------------------------
# Repo map build + ranking + truncation + filtering
# ---------------------------------------------------------------------------
class TestBuildRepoMap:
    def _make_pkg(self, root):
        # ``hot`` is referenced many times across the package; ``cold`` once.
        _write(
            root,
            "pkg/core.py",
            "def hot():\n    return 1\n\ndef cold():\n    return 0\n",
        )
        _write(
            root,
            "pkg/a.py",
            "from pkg.core import hot\n\ndef a():\n    return hot() + hot()\n",
        )
        _write(
            root,
            "pkg/b.py",
            "from pkg.core import hot\n\ndef b():\n    return hot()\n",
        )

    def test_ref_count_rank_orders_hot_symbols_first(self, tmp_path):
        self._make_pkg(tmp_path)
        result = build_repo_map(str(tmp_path))
        names = [s["name"] for s in result["symbols"]]
        # ``hot`` (referenced 3x) must rank ahead of ``cold`` (0 refs).
        assert names.index("hot") < names.index("cold")
        hot = next(s for s in result["symbols"] if s["name"] == "hot")
        assert hot["ref_count"] >= 3

    def test_budget_truncation_caps_output(self, tmp_path):
        # Generate many symbols, then cap to a small budget.
        lines = "".join(f"def fn_{i}():\n    pass\n\n" for i in range(50))
        _write(tmp_path, "many.py", lines)

        full = build_repo_map(str(tmp_path), max_symbols=1000)
        capped = build_repo_map(str(tmp_path), max_symbols=10)

        assert len(full["symbols"]) == 50
        assert len(capped["symbols"]) == 10
        assert capped["truncated"] is True
        assert capped["total_symbols"] == 50
        assert full["truncated"] is False

    def test_non_python_files_ignored(self, tmp_path):
        _write(tmp_path, "code.py", "def real():\n    pass\n")
        _write(tmp_path, "notes.txt", "def fake():\n    pass\n")
        _write(tmp_path, "data.json", '{"def": "x"}')
        result = build_repo_map(str(tmp_path))
        names = {s["name"] for s in result["symbols"]}
        assert "real" in names
        assert "fake" not in names

    def test_syntax_error_file_skipped_not_fatal(self, tmp_path):
        _write(tmp_path, "good.py", "def good():\n    pass\n")
        _write(tmp_path, "bad.py", "def bad(:\n    pass\n")
        result = build_repo_map(str(tmp_path))
        names = {s["name"] for s in result["symbols"]}
        assert "good" in names
        # The broken file contributes nothing but does not crash the build.
        assert "bad" not in names

    def test_excludes_dot_dirs_venv_node_modules_pycache(self, tmp_path):
        _write(tmp_path, "app.py", "def app():\n    pass\n")
        _write(tmp_path, ".git/hooks/x.py", "def gitfn():\n    pass\n")
        _write(tmp_path, "venv/lib/v.py", "def venvfn():\n    pass\n")
        _write(tmp_path, ".venv/lib/v2.py", "def dotvenvfn():\n    pass\n")
        _write(tmp_path, "node_modules/m.py", "def nmfn():\n    pass\n")
        _write(tmp_path, "__pycache__/c.py", "def pycfn():\n    pass\n")
        result = build_repo_map(str(tmp_path))
        names = {s["name"] for s in result["symbols"]}
        assert "app" in names
        for excluded in ("gitfn", "venvfn", "dotvenvfn", "nmfn", "pycfn"):
            assert excluded not in names

    def test_nonexistent_path_errors_gracefully(self, tmp_path):
        missing = tmp_path / "does_not_exist"
        result = build_repo_map(str(missing))
        assert "error" in result


# ---------------------------------------------------------------------------
# Tool handler + registration (mirrors file_tools wiring)
# ---------------------------------------------------------------------------
class TestRepoMapTool:
    def test_handler_returns_json_string(self, tmp_path):
        _write(tmp_path, "x.py", "def x():\n    pass\n")
        out = _handle_repo_map({"path": str(tmp_path)})
        data = json.loads(out)
        assert "symbols" in data
        assert any(s["name"] == "x" for s in data["symbols"])

    def test_handler_respects_max_symbols_arg(self, tmp_path):
        lines = "".join(f"def fn_{i}():\n    pass\n\n" for i in range(20))
        _write(tmp_path, "m.py", lines)
        out = _handle_repo_map({"path": str(tmp_path), "max_symbols": 5})
        data = json.loads(out)
        assert len(data["symbols"]) == 5
        assert data["truncated"] is True

    def test_schema_shape(self):
        assert REPO_MAP_SCHEMA["name"] == "repo_map"
        assert "path" in REPO_MAP_SCHEMA["parameters"]["properties"]
        assert "max_symbols" in REPO_MAP_SCHEMA["parameters"]["properties"]

    def test_tool_is_registered_like_other_file_tools(self):
        # Importing tools.repo_map runs the module-level registry.register(),
        # exactly as tools.file_tools does for read_file/search_files.
        import tools.repo_map  # noqa: F401
        from tools.registry import registry

        entry = registry.get_entry("repo_map")
        assert entry is not None
        assert entry.toolset == "file"
        assert registry.get_schema("repo_map")["name"] == "repo_map"
