"""Network diagnostics remain callable without starting sandbox/network fixtures."""

import ast
from pathlib import Path


def test_zero_timeout_port_diagnostic_has_its_clock_import():
    source = Path(__file__).with_name("_netedge.py")
    tree = ast.parse(source.read_text(encoding="utf-8-sig"))
    # Load the real helper and its clock import without importing _helpers, whose
    # module initialization probes network-namespace availability.
    nodes = [
        node for node in tree.body
        if (isinstance(node, ast.Import) and any(alias.name == "time" for alias in node.names))
        or (isinstance(node, ast.FunctionDef) and node.name == "wait_port_free")
    ]
    namespace = {}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(source), "exec"), namespace)
    assert namespace["wait_port_free"](port=0, timeout=0) is None
