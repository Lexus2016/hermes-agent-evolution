"""PyYAML is not a dependency: the module must import, and `yaml` must still resolve.

`tools/skills_hub` re-exported PyYAML through the plugin-compat block. PyYAML is not
declared anywhere (the repo's YAML policy is `hermes_yaml`/ruamel), so a clean install
could not import the module at all and whole test slices collapsed with
`ModuleNotFoundError: No module named 'yaml'`.
"""

import subprocess
import sys
import textwrap
from pathlib import Path

import hermes_yaml

from tools import skills_hub

ROOT = Path(__file__).resolve().parents[2]


def test_legacy_yaml_name_resolves_to_the_policy_module():
    assert skills_hub.yaml is hermes_yaml
    assert skills_hub.yaml.safe_load("a: 1\n") == {"a": 1}


def test_module_imports_when_pyyaml_is_unavailable():
    """The regression that broke clean installs: import with `yaml` blocked."""
    probe = textwrap.dedent(
        """
        import importlib.abc, sys

        class _BlockPyYAML(importlib.abc.MetaPathFinder):
            def find_spec(self, fullname, path=None, target=None):
                if fullname == "yaml" or fullname.startswith("yaml."):
                    raise ImportError("No module named 'yaml' (blocked on purpose)")
                return None

        sys.meta_path.insert(0, _BlockPyYAML())
        import tools.skills_hub as hub

        assert hub.yaml.safe_load("a: 1\\n") == {"a": 1}, hub.yaml
        print("ok")
        """
    )
    proc = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert "ok" in proc.stdout
