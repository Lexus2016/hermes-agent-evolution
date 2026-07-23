#!/usr/bin/env python3
"""CFG/DFG extractor for Python source files (issue #1221).

Increment 1 of 3 for #1221 (parent #1180 — IFG monitor for covert
sabotage detection). Extracts control-flow and data-flow graphs from
Python source files using ``ast.parse``.

Callable standalone via CLI:
    python -m scripts.evolution_cfg_dfg_extractor --file path/to/module.py
    python -m scripts.evolution_cfg_dfg_extractor --diff HEAD~1 --repo /path

Increments 2 (diff + flagging) and 3 (integration wiring) will consume
the structured graph output.
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional


def extract_cfg_dfg(source: str, filename: str = "<string>") -> Dict[str, Any]:
    """Extract control-flow and data-flow graph from Python source.

    Returns a dict with:
        - ``file``: filename
        - ``functions``: list of per-function dicts with ``name``, ``lineno``,
          ``branches`` (list of {type, lineno}), ``calls`` (list of names),
          ``assignments`` (list of {target, lineno}), ``returns`` (list of linenos)
    """
    try:
        tree = ast.parse(source, filename=filename)
    except SyntaxError as e:
        return {"file": filename, "error": f"SyntaxError: {e}", "functions": []}

    functions: List[Dict[str, Any]] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            functions.append(_extract_function(node))
    return {"file": filename, "functions": functions}


def _extract_function(
    func_node: ast.FunctionDef | ast.AsyncFunctionDef,
) -> Dict[str, Any]:
    """Extract CFG/DFG data for a single function."""
    branches: List[Dict[str, Any]] = []
    calls: List[str] = []
    assignments: List[Dict[str, Any]] = []
    returns: List[int] = []

    for node in ast.walk(func_node):
        if isinstance(
            node, (ast.If, ast.For, ast.While, ast.With, ast.Try, ast.ExceptHandler)
        ):
            branches.append({
                "type": type(node).__name__,
                "lineno": node.lineno,
            })
        elif isinstance(node, ast.Call):
            name = _get_call_name(node)
            if name:
                calls.append(name)
        elif isinstance(node, ast.Assign):
            target = _get_target_name(node)
            if target:
                assignments.append({"target": target, "lineno": node.lineno})
        elif isinstance(node, ast.Return):
            returns.append(node.lineno)

    return {
        "name": func_node.name,
        "lineno": func_node.lineno,
        "branches": branches,
        "calls": calls,
        "assignments": assignments,
        "returns": returns,
    }


def _get_call_name(call: ast.Call) -> str:
    """Extract a readable name from a Call node."""
    func = call.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return f"{_get_attribute_chain(func)}.{func.attr}"
    return ""


def _get_attribute_chain(attr: ast.Attribute) -> str:
    if isinstance(attr.value, ast.Name):
        return attr.value.id
    if isinstance(attr.value, ast.Attribute):
        return _get_attribute_chain(attr.value)
    return "..."


def _get_target_name(assign: ast.Assign) -> str:
    """Extract the target variable name from an Assign node."""
    if len(assign.targets) == 1:
        target = assign.targets[0]
        if isinstance(target, ast.Name):
            return target.id
        if isinstance(target, ast.Attribute):
            chain = _get_attribute_chain(target)
            return f"{chain}.{target.attr}"
    return ""


def extract_from_file(filepath: str) -> Dict[str, Any]:
    """Extract CFG/DFG from a Python file on disk."""
    path = Path(filepath)
    if not path.exists():
        return {"file": filepath, "error": "File not found", "functions": []}
    source = path.read_text(encoding="utf-8")
    return extract_cfg_dfg(source, filename=filepath)


def extract_from_diff(
    base: str = "HEAD~1",
    repo: str = ".",
) -> List[Dict[str, Any]]:
    """Extract CFG/DFG for all changed Python files in a git diff.

    Returns a list of per-file extraction results.
    """
    try:
        result = subprocess.run(
            ["git", "diff", "--name-only", base],
            capture_output=True,
            text=True,
            cwd=repo,
            timeout=30,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        return [{"error": f"git diff failed: {e}"}]

    changed_files = [f for f in result.stdout.strip().splitlines() if f.endswith(".py")]
    results: List[Dict[str, Any]] = []
    for f in changed_files:
        full_path = os.path.join(repo, f)
        if os.path.exists(full_path):
            entry = extract_from_file(full_path)
            entry["file"] = f
            results.append(entry)
    return results


def main(argv: Optional[List[str]] = None) -> int:
    """CLI entry point for CFG/DFG extraction."""
    parser = argparse.ArgumentParser(
        description="Extract control-flow and data-flow graphs from Python source.",
    )
    parser.add_argument("--file", help="Python file to analyze")
    parser.add_argument(
        "--diff", metavar="BASE", help="Analyze changed .py files in git diff from BASE"
    )
    parser.add_argument("--repo", default=".", help="Repository root for --diff")
    parser.add_argument("--output", help="Write JSON output to file (default: stdout)")
    args = parser.parse_args(argv)

    if not args.file and not args.diff:
        parser.print_help()
        return 1

    if args.file:
        result = extract_from_file(args.file)
        output = json.dumps(result, indent=2, default=str)
    else:
        results = extract_from_diff(base=args.diff, repo=args.repo)
        output = json.dumps(results, indent=2, default=str)

    if args.output:
        Path(args.output).write_text(output, encoding="utf-8")
        print(f"Written to {args.output}")
    else:
        print(output)
    return 0


if __name__ == "__main__":
    sys.exit(main())
