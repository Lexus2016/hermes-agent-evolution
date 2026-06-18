"""Regression tests for memory-tool import fallbacks."""

import builtins
import importlib
import sys

from tools.registry import registry


def test_memory_tool_imports_without_fcntl(monkeypatch, tmp_path):
    original_import = builtins.__import__
    # Snapshot the real module so we can restore the EXACT object afterwards.
    # Without this, the reimport below leaves a *different* tools.memory_tool
    # object in sys.modules; later test files that string-path patch
    # "tools.memory_tool.get_memory_dir" would then patch the wrong object
    # (a latent cross-file ordering bug, surfaced when this file is collected
    # before test_memory_tool.py).
    original_module = sys.modules.get("tools.memory_tool")

    def fake_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "fcntl":
            raise ImportError("simulated missing fcntl")
        return original_import(name, globals, locals, fromlist, level)

    registry.deregister("memory")
    monkeypatch.delitem(sys.modules, "tools.memory_tool", raising=False)
    monkeypatch.setattr(builtins, "__import__", fake_import)

    memory_tool = importlib.import_module("tools.memory_tool")
    monkeypatch.setattr(memory_tool, "get_memory_dir", lambda: tmp_path)

    store = memory_tool.MemoryStore(memory_char_limit=200, user_char_limit=200)
    store.load_from_disk()
    result = store.add("memory", "fact learned during import fallback test")

    assert memory_tool.fcntl is None
    assert registry.get_entry("memory") is not None
    assert result["success"] is True

    # Restore the ORIGINAL module object after the reimport. The reimport above
    # created a *new* tools.memory_tool object and rebound TWO references to it:
    #   1. sys.modules["tools.memory_tool"]
    #   2. the parent-package attribute `tools.memory_tool`
    # monkeypatch does not track either rebind. Critically, pytest's
    # monkeypatch.setattr("tools.memory_tool.<attr>", ...) resolves the target
    # via getattr(tools, "memory_tool") — the PARENT-PACKAGE attribute — so if
    # that still points at the discarded reimported object, later test files'
    # string-path patches silently hit the wrong module and never take effect
    # (a latent cross-file ordering bug, surfaced when this file is collected
    # before test_memory_tool.py). Rebind both back to the untouched original;
    # no reload (which would swap class identities and break collection-time
    # `from tools.memory_tool import MemoryStore` bindings in other files).
    if original_module is not None:
        sys.modules["tools.memory_tool"] = original_module
        import tools as _tools_pkg
        _tools_pkg.memory_tool = original_module
