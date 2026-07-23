#!/usr/bin/env python3
"""Control-flow / data-flow graph extraction from Python source (#1221).

Extracts function-level control-flow (branches, calls, returns) and
data-flow (variable assignments, argument passing) graphs from Python
source files using the stdlib ``ast`` module.

WHY THIS EXISTS — IFG monitor for covert sabotage detection (parent #1180).
When a PR introduces a subtle sabotage (e.g. a deleted error check, a
silently-swapped argument), a diff alone may not surface the semantic
change.  Extracting the control-flow and data-flow graph from the changed
files provides the structured representation that downstream increments
(diff + flagging, integration wiring) compare against a baseline to detect
semantic drift.

DESIGN — pure stdlib (ast), no external dependencies.
``extract_graphs`` takes Python source code (string) and returns a
structured JSON-serializable dict per function.  ``extract_graphs_from_diff``
takes a unified diff and extracts graphs from only the changed Python files.

Output schema per function::

    {
        "function": "my_func",
        "lineno": 10,
        "end_lineno": 20,
        "control_flow": {
            "branches": [{"type": "if", "lineno": 12}, ...],
            "calls": [{"name": "print", "lineno": 15}, ...],
            "returns": [{"lineno": 18}, ...],
        },
        "data_flow": {
            "assignments": [{"target": "x", "lineno": 11}, ...],
            "arg_passing": [{"func": "foo", "arg": "y", "lineno": 14}, ...],
        }
    }
"""

from __future__ import annotations

import ast
import json
import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class _GraphExtractor(ast.NodeVisitor):
    """AST visitor that accumulates control-flow and data-flow entries."""

    def __init__(self) -> None:
        self.branches: List[Dict[str, Any]] = []
        self.calls: List[Dict[str, Any]] = []
        self.returns: List[Dict[str, Any]] = []
        self.assignments: List[Dict[str, Any]] = []
        self.arg_passing: List[Dict[str, Any]] = []

    # --- Control flow ---

    def visit_If(self, node: ast.If) -> None:
        self.branches.append({"type": "if", "lineno": node.lineno})
        self.generic_visit(node)

    def visit_For(self, node: ast.For) -> None:
        self.branches.append({"type": "for", "lineno": node.lineno})
        self.generic_visit(node)

    def visit_While(self, node: ast.While) -> None:
        self.branches.append({"type": "while", "lineno": node.lineno})
        self.generic_visit(node)

    def visit_Try(self, node: ast.Try) -> None:
        self.branches.append({"type": "try", "lineno": node.lineno})
        self.generic_visit(node)

    def visit_With(self, node: ast.With) -> None:
        self.branches.append({"type": "with", "lineno": node.lineno})
        self.generic_visit(node)

    def visit_Assert(self, node: ast.Assert) -> None:
        self.branches.append({"type": "assert", "lineno": node.lineno})
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        func_name = self._call_name(node.func)
        self.calls.append({"name": func_name, "lineno": node.lineno})
        # Record argument passing for data-flow
        for arg in node.args:
            if isinstance(arg, ast.Name):
                self.arg_passing.append({
                    "func": func_name,
                    "arg": arg.id,
                    "lineno": node.lineno,
                })
        for kw in node.keywords:
            if kw.value and isinstance(kw.value, ast.Name):
                self.arg_passing.append({
                    "func": func_name,
                    "arg": f"{kw.arg}={kw.value.id}",
                    "lineno": node.lineno,
                })
        self.generic_visit(node)

    def visit_Return(self, node: ast.Return) -> None:
        self.returns.append({"lineno": node.lineno})
        self.generic_visit(node)

    # --- Data flow ---

    def visit_Assign(self, node: ast.Assign) -> None:
        for target in node.targets:
            name = self._target_name(target)
            if name:
                self.assignments.append({"target": name, "lineno": node.lineno})
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        name = self._target_name(node.target)
        if name:
            self.assignments.append({"target": name, "lineno": node.lineno})
        self.generic_visit(node)

    def visit_AugAssign(self, node: ast.AugAssign) -> None:
        name = self._target_name(node.target)
        if name:
            self.assignments.append({"target": name, "lineno": node.lineno})
        self.generic_visit(node)

    # --- Helpers ---

    @staticmethod
    def _call_name(node: ast.expr) -> str:
        """Extract a readable name from a Call's func node."""
        if isinstance(node, ast.Name):
            return node.id
        if isinstance(node, ast.Attribute):
            prefix = _GraphExtractor._call_name(node.value)
            return f"{prefix}.{node.attr}" if prefix else node.attr
        if isinstance(node, ast.Subscript):
            base = _GraphExtractor._call_name(node.value)
            return f"{base}[...]" if base else "[...]"
        return "<?>"

    @staticmethod
    def _target_name(node: ast.expr) -> Optional[str]:
        """Extract the variable name from an assignment target."""
        if isinstance(node, ast.Name):
            return node.id
        if isinstance(node, ast.Tuple) and node.elts:
            names = [_GraphExtractor._target_name(e) for e in node.elts]
            return ", ".join(n for n in names if n)
        if isinstance(node, ast.Attribute):
            prefix = _GraphExtractor._call_name(node.value)
            return f"{prefix}.{node.attr}" if prefix else node.attr
        return None


def extract_function_graph(
    func_node: ast.FunctionDef | ast.AsyncFunctionDef,
) -> Dict[str, Any]:
    """Extract a control-flow/data-flow graph for a single function.

    Args:
        func_node: An ``ast.FunctionDef`` or ``ast.AsyncFunctionDef`` node.

    Returns:
        A dict with the function name, line range, control_flow, and data_flow.
    """
    extractor = _GraphExtractor()
    extractor.visit(func_node)
    return {
        "function": func_node.name,
        "lineno": func_node.lineno,
        "end_lineno": getattr(func_node, "end_lineno", func_node.lineno),
        "control_flow": {
            "branches": extractor.branches,
            "calls": extractor.calls,
            "returns": extractor.returns,
        },
        "data_flow": {
            "assignments": extractor.assignments,
            "arg_passing": extractor.arg_passing,
        },
    }


def extract_graphs(source: str) -> List[Dict[str, Any]]:
    """Extract control-flow/data-flow graphs from Python source code.

    Args:
        source: Python source code as a string.

    Returns:
        A list of per-function graph dicts.  Functions with syntax errors
        produce an empty list (the error is logged, not raised).
    """
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        logger.warning("Syntax error parsing source: %s", exc)
        return []

    graphs: List[Dict[str, Any]] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            graphs.append(extract_function_graph(node))
    return graphs


def extract_graphs_from_file(path: Path) -> List[Dict[str, Any]]:
    """Extract graphs from a Python file on disk.

    Args:
        path: Path to a ``.py`` file.

    Returns:
        A list of per-function graph dicts.
    """
    try:
        source = path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning("Failed to read %s: %s", path, exc)
        return []
    graphs = extract_graphs(source)
    for g in graphs:
        g["file"] = str(path)
    return graphs


# Regex to extract file paths from unified diff headers.
_DIFF_FILE_RE = re.compile(r"^diff --git a/(.+?) b/(.+)$", re.MULTILINE)


def extract_graphs_from_diff(
    diff_text: str, repo_root: Optional[Path] = None
) -> Dict[str, List[Dict[str, Any]]]:
    """Extract graphs from Python files changed in a unified diff.

    Args:
        diff_text: Unified diff text (e.g. from ``git diff``).
        repo_root: Root directory to resolve file paths from.  If None,
                   uses the current working directory.

    Returns:
        A dict mapping changed Python file paths to their graph lists.
        Only ``.py`` files are included.
    """
    root = repo_root or Path.cwd()
    result: Dict[str, List[Dict[str, Any]]] = {}

    for match in _DIFF_FILE_RE.finditer(diff_text):
        # Use the "b/" side (new file path)
        file_path = match.group(2)
        if not file_path.endswith(".py"):
            continue
        full_path = root / file_path
        if not full_path.exists():
            logger.debug("Changed file not found on disk: %s", full_path)
            continue
        graphs = extract_graphs_from_file(full_path)
        if graphs:
            result[file_path] = graphs

    return result


def graphs_to_json(
    graphs: Dict[str, List[Dict[str, Any]]] | List[Dict[str, Any]],
) -> str:
    """Serialize graph(s) to a JSON string.

    Args:
        graphs: Either a list of per-function graphs (from ``extract_graphs``)
                or a dict from file paths to graph lists (from
                ``extract_graphs_from_diff``).

    Returns:
        A JSON string.
    """
    return json.dumps(graphs, ensure_ascii=False, indent=2)
