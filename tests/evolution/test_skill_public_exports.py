"""Public skill exports remain importable after integration lint repairs."""

import importlib

import pytest


@pytest.mark.parametrize("module_name", [
    "evolution.lib.skill_reuse_gate", "evolution.lib.skill_shrink",
])
def test_declared_skill_exports_are_importable(module_name):
    module = importlib.import_module(module_name)
    namespace = {}
    exec(f"from {module_name} import *", namespace)
    for name in module.__all__:
        assert namespace[name] is getattr(module, name)
