"""Retained fork memory providers cannot be replaced by update-time catalog migration."""
from pathlib import Path

import pytest

from hermes_cli import memory_provider_migration
from plugins.memory import find_provider_dir


@pytest.mark.parametrize("provider", ["honcho", "mem0", "hindsight"])
def test_bundled_provider_wins_and_update_does_not_install_catalog(tmp_path, monkeypatch, provider):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text(f"memory:\n  provider: {provider}\n", encoding="utf-8")
    shadow = tmp_path / "plugins" / provider
    shadow.mkdir(parents=True)
    (shadow / "__init__.py").write_text("class Shadow(MemoryProvider): pass\n", encoding="utf-8")
    selected = find_provider_dir(provider)
    assert selected == Path(__file__).resolve().parents[3] / "plugins" / "memory" / provider
    assert memory_provider_migration.migrate_home(
        tmp_path,
        install=lambda name: pytest.fail(f"bundled provider must not install {name}"),
        say=lambda message: pytest.fail(message),
    ) is None
