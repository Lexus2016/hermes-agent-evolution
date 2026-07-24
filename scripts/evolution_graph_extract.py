#!/usr/bin/env python3
"""Control-flow/data-flow graph extraction from Python code (issue #1221, child of #1180).

ast.parse-based graph extraction: per-function control-flow (branches, calls,
returns, raises, loops) and data-flow (assignments, params, returns). Output
structured JSON. Read-only analysis, no behavioral change.
"""

from __future__ import annotations

import ast
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Union

__all__ = [
    "ControlFlowEdge",
    "DataFlowEdge",
    "FunctionGraph",
    "FileGraph",
    "extract_file_graph",
    "extract_graph",
    "graph_to_json",
    "main",
]


@dataclass
class ControlFlowEdge:
    type: str
    line: int
    detail: str = ""

    def to_dict(self) -> Dict[str, Any]:
        d = {"type": self.type, "line": self.line}
        if self.detail:
            d["detail"] = self.detail
        return d


@dataclass
class DataFlowEdge:
    type: str
    line: int
    target: str = ""
    source: str = ""

    def to_dict(self) -> Dict[str, Any]:
        d = {"type": self.type, "line": self.line}
        if self.target:
            d["target"] = self.target
        if self.source:
            d["source"] = self.source
        return d


@dataclass
class FunctionGraph:
    name: str
    line: int
    control_flow: List[ControlFlowEdge] = field(default_factory=list)
    data_flow: List[DataFlowEdge] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "line": self.line,
            "control_flow": [e.to_dict() for e in self.control_flow],
            "data_flow": [e.to_dict() for e in self.data_flow],
        }


@dataclass
class FileGraph:
    path: str
    functions: List[FunctionGraph] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {"path": self.path, "functions": [f.to_dict() for f in self.functions]}


class _Extractor(ast.NodeVisitor):
    """AST visitor extracting control/data-flow edges per function."""

    def __init__(self) -> None:
        self.current_func: Optional[FunctionGraph] = None
        self._functions: List[FunctionGraph] = []

    def _txt(self, node: ast.expr) -> str:
        try:
            return ast.unparse(node)
        except Exception:
            return type(node).__name__

    def _handle_func(self, node: Union[ast.FunctionDef, ast.AsyncFunctionDef]) -> None:
        parent = self.current_func
        fg = FunctionGraph(name=node.name, line=node.lineno)
        self.current_func = fg
        for arg in node.args.args:
            fg.data_flow.append(
                DataFlowEdge(type="param", line=node.lineno, target=arg.arg)
            )
        for stmt in node.body:
            self.visit(stmt)
        self._functions.append(fg)
        self.current_func = parent

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._handle_func(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._handle_func(node)

    def visit_If(self, node: ast.If) -> None:
        if self.current_func:
            self.current_func.control_flow.append(
                ControlFlowEdge(
                    type="branch", line=node.lineno, detail=self._txt(node.test)
                )
            )
        self.generic_visit(node)

    def visit_For(self, node: ast.For) -> None:
        if self.current_func:
            self.current_func.control_flow.append(
                ControlFlowEdge(
                    type="loop", line=node.lineno, detail=self._txt(node.iter)
                )
            )
            self.current_func.data_flow.append(
                DataFlowEdge(
                    type="assign",
                    line=node.lineno,
                    target=self._txt(node.target),
                    source=self._txt(node.iter),
                )
            )
        self.generic_visit(node)

    def visit_While(self, node: ast.While) -> None:
        if self.current_func:
            self.current_func.control_flow.append(
                ControlFlowEdge(
                    type="loop", line=node.lineno, detail=self._txt(node.test)
                )
            )
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        if self.current_func:
            target = self._txt(node.func)
            args = [self._txt(a) for a in node.args]
            self.current_func.control_flow.append(
                ControlFlowEdge(
                    type="call", line=node.lineno, detail=f"{target}({', '.join(args)})"
                )
            )
        self.generic_visit(node)

    def visit_Return(self, node: ast.Return) -> None:
        if self.current_func:
            val = self._txt(node.value) if node.value else "None"
            self.current_func.control_flow.append(
                ControlFlowEdge(type="return", line=node.lineno, detail=val)
            )
            self.current_func.data_flow.append(
                DataFlowEdge(type="return_value", line=node.lineno, source=val)
            )
        self.generic_visit(node)

    def visit_Raise(self, node: ast.Raise) -> None:
        if self.current_func:
            self.current_func.control_flow.append(
                ControlFlowEdge(
                    type="raise",
                    line=node.lineno,
                    detail=self._txt(node.exc) if node.exc else "re-raise",
                )
            )
        self.generic_visit(node)

    def visit_Assign(self, node: ast.Assign) -> None:
        if self.current_func:
            targets = ", ".join(self._txt(t) for t in node.targets)
            self.current_func.data_flow.append(
                DataFlowEdge(
                    type="assign",
                    line=node.lineno,
                    target=targets,
                    source=self._txt(node.value),
                )
            )
        self.generic_visit(node)

    def visit_AugAssign(self, node: ast.AugAssign) -> None:
        if self.current_func:
            self.current_func.data_flow.append(
                DataFlowEdge(
                    type="aug_assign",
                    line=node.lineno,
                    target=self._txt(node.target),
                    source=self._txt(node.value),
                )
            )
        self.generic_visit(node)

    def extract(self, tree: ast.AST) -> List[FunctionGraph]:
        self._functions = []
        self.current_func = None
        self.visit(tree)
        return self._functions


def extract_file_graph(source: str, path: str = "<string>") -> Optional[FileGraph]:
    try:
        tree = ast.parse(source, filename=path)
    except SyntaxError:
        return None
    return FileGraph(path=path, functions=_Extractor().extract(tree))


def extract_graph(paths: Sequence[Union[str, Path]]) -> Dict[str, Any]:
    files_data, errors = [], []
    for p in paths:
        path = Path(p)
        if not path.is_file():
            errors.append(str(path))
            continue
        try:
            src = path.read_text(encoding="utf-8")
        except OSError:
            errors.append(str(path))
            continue
        g = extract_file_graph(src, str(path))
        if g is None:
            errors.append(str(path))
            continue
        files_data.append(g.to_dict())
    return {"files": files_data, "errors": errors}


def graph_to_json(g: Dict[str, Any]) -> str:
    return json.dumps(g, indent=2, sort_keys=True)


def main(argv: List[str]) -> int:
    args = argv[1:]
    paths = [a for a in args if not a.startswith("--")]
    if not paths:
        print("usage: evolution_graph_extract.py <file.py> [...]", file=sys.stderr)
        return 2
    print(graph_to_json(extract_graph(paths)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
