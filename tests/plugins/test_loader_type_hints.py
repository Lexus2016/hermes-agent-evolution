"""Discovery contracts remain introspectable without importing provider implementations."""

import importlib
import subprocess
import sys
from pathlib import Path
from typing import get_type_hints

import pytest


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)


@pytest.mark.parametrize("loader,contract,names", [
    ("plugins.context_engine", "agent.context_engine.ContextEngine",
     ("load_context_engine", "_load_engine_from_dir")),
])
def test_loader_annotations_resolve_to_canonical_contract(loader, contract, names):
    module = importlib.import_module(loader)
    owner, symbol = contract.rsplit(".", 1)
    base = getattr(importlib.import_module(owner), symbol)
    for name in names:
        assert get_type_hints(getattr(module, name))["return"] == base | None


@pytest.mark.parametrize("first,second", [
    ("plugins.context_engine", "agent.context_engine"),
    ("agent.context_engine", "plugins.context_engine"),
])
def test_cold_loader_import_order_keeps_providers_lazy(first, second):
    source = """
import importlib, sys
for name in sys.argv[1:]:
    importlib.import_module(name)
assert 'plugins.context_engine.compressor' not in sys.modules
assert 'cron.scheduler' not in sys.modules
assert not any(name.startswith('plugins.cron_providers.') for name in sys.modules)
"""
    result = subprocess.run([sys.executable, "-c", source, first, second],
                            capture_output=True, text=True, encoding="utf-8", errors="replace",
                            timeout=20, check=False)
    assert result.returncode == 0, result.stderr
