"""Guard the seams the test suite patches, and the compat-layer names modules import.

Two drifts from the v0.22.2 sync sat undetected for days and took five Python slices red:

* 16 files patched ``run_agent.get_tool_definitions`` / ``run_agent.check_toolset_requirements`` /
  ``run_agent.OpenAI`` — names ``run_agent`` no longer exports, so every one of them raised
  ``AttributeError`` before the test body ran;
* seven PEP-562 shims imported ``warn_once`` from ``hermes_cli.plugin_compat`` after that module was
  reduced to three inert stubs, so resolving any legacy alias raised ``ImportError``.

Both fail loudly here instead of silently reddening a slice. The run_agent check reads the module's
source rather than importing it: importing run_agent pulls the whole agent stack (~40s) and this
guard has to stay cheap enough to run on every PR. run_agent has no PEP-562 ``__getattr__``, so its
top-level bindings are its real attribute namespace.
"""

from __future__ import annotations

import ast
import os
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
TESTS = ROOT / "tests"
RUN_AGENT = ROOT / "run_agent.py"
PLUGIN_COMPAT = "hermes_cli.plugin_compat"

# Directories that never hold first-party source: walking them makes the scan minutes long.
SKIP_DIRS = {
    ".git", ".venv", "venv", "node_modules", "__pycache__", ".mypy_cache", ".pytest_cache",
    ".ruff_cache", "web", "ui-tui", "native", "assets", "contributors",
}

# A patch target is a dotted attribute path, never a filename ("run_agent.py").
RUN_AGENT_TARGET = re.compile(r"^run_agent\.([A-Za-z_][A-Za-z0-9_]*)$")

# Sanity anchors: if the source scan stops finding these, the scanner itself is broken.
KNOWN_EXPORTS = {"logger", "AIAgent"}


def _python_files(base: Path) -> list[Path]:
    found: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        found.extend(Path(dirpath) / name for name in filenames if name.endswith(".py"))
    return found


def _module_namespace(path: Path) -> set[str]:
    """Top-level names a module binds: defs, classes, assignments, imports."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Import):
            names.update(alias.asname or alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            names.update(alias.asname or alias.name for alias in node.names)
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Name):
                    names.add(target.id)
                elif isinstance(target, (ast.Tuple, ast.List)):
                    names.update(elt.id for elt in target.elts if isinstance(elt, ast.Name))
    return names


def _patch_targets() -> dict[str, list[str]]:
    """``run_agent.<name>`` strings used in the test suite."""
    found: dict[str, list[str]] = {}
    for path in _python_files(TESTS):
        text = path.read_text(encoding="utf-8", errors="replace")
        if '"run_agent.' not in text and "'run_agent." not in text:
            continue  # cheap pre-filter: parsing every test file is what makes this slow
        try:
            tree = ast.parse(text)
        except SyntaxError:  # a broken test file is some other test's problem
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
                continue
            if node.value.endswith(".py"):  # a filename, not an attribute
                continue
            match = RUN_AGENT_TARGET.match(node.value)
            if match:
                found.setdefault(match.group(1), []).append(str(path.relative_to(ROOT)))
    return found


def test_patch_targets_on_run_agent_exist() -> None:
    """Patching a name run_agent does not export fails before the test body — catch it here."""
    namespace = _module_namespace(RUN_AGENT)
    assert KNOWN_EXPORTS <= namespace, "run_agent source scan found %d names, missing anchors" % len(namespace)

    targets = _patch_targets()
    assert targets, "expected the agent tests to patch run_agent.*; scanner is broken"

    missing = {
        name: sorted(set(files))
        for name, files in targets.items()
        if name not in namespace
    }
    assert not missing, "tests patch run_agent attributes that do not exist: " + ", ".join(
        "%s (%s)" % (name, ", ".join(files[:3])) for name, files in sorted(missing.items())
    )


def test_plugin_compat_imports_resolve() -> None:
    """Every name imported from the plugin-compat module must still be exported by it."""
    module = pytest.importorskip(PLUGIN_COMPAT)
    offenders: list[str] = []
    for path in _python_files(ROOT):
        text = path.read_text(encoding="utf-8", errors="replace")
        if 'plugin_compat' not in text:
            continue  # cheap pre-filter: the import is rare, parsing the whole tree is not
        try:
            tree = ast.parse(text)
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom) or node.module != PLUGIN_COMPAT:
                continue
            for alias in node.names:
                if not hasattr(module, alias.name):
                    offenders.append("%s: %s" % (path.relative_to(ROOT), alias.name))
    assert not offenders, "%s no longer exports: %s" % (PLUGIN_COMPAT, ", ".join(sorted(offenders)))
