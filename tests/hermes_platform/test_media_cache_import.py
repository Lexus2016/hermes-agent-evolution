"""Shared media policy imports do not initialize gateway/config/profile state."""

import os
import subprocess
import sys
from pathlib import Path


def test_cold_media_policy_import_is_inert(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    script = """
import os, sys
from pathlib import Path
before = sorted(str(p) for p in Path(os.environ['HOME']).rglob('*'))
from hermes_platform import media_cache_privacy
assert 'gateway' not in sys.modules
assert 'hermes_cli.config' not in sys.modules
assert sorted(str(p) for p in Path(os.environ['HOME']).rglob('*')) == before
"""
    result = subprocess.run([sys.executable, "-c", script], env=dict(os.environ),
                            capture_output=True, text=True, encoding="utf-8", errors="replace",
                            check=False, timeout=20)
    assert result.returncode == 0, result.stderr
