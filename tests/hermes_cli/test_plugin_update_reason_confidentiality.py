import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
import pytest

@pytest.mark.parametrize('kind', ['git_timeout', 'legacy_feed'])
def test_synthetic_secret_absent_from_actual_receipt_and_cli(tmp_path, monkeypatch, capsys, caplog, kind):
    import hermes_cli.plugins_cmd as pc
    import hermes_cli.plugins_updates as updates
    from hermes_cli.plugins_cmd_update import cmd_check_updates
    from hermes_cli.plugins_cadence import run_scheduled_check
    from pm import receipt
    home = tmp_path / '.hermes'
    plug = home / 'plugins' / 'probe'
    (plug / '.git').mkdir(parents=True)
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.setenv('HERMES_HOME', str(home))
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    marker = 'SYNTHETIC_ONLY_TOKEN_7482'
    row = {'source': f'https://user:{marker}@example.invalid/repo', 'revision': 'a' * 40}
    manifest = 'name: probe\n'
    if kind == 'legacy_feed':
        row['update_url'] = f'http://example.invalid/feed?token={marker}'
        manifest += f"update_url: {row['update_url']}\n"
    else:
        def timeout(argv, **kwargs):
            raise subprocess.TimeoutExpired(argv, kwargs['timeout'])
        monkeypatch.setattr(updates.subprocess, 'run', timeout)
        monkeypatch.setattr(pc, '_resolve_git_executable', lambda: 'git')
    (plug / 'plugin.yaml').write_text(manifest, encoding='utf-8')
    (plug.parent / '.install-metadata.json').write_text(json.dumps({'probe': row}), encoding='utf-8')
    monkeypatch.setattr(pc, '_plugins_dir', lambda: plug.parent)
    class EmptyEntryPoints:
        def select(self, **kwargs):
            return []
    monkeypatch.setattr(updates.importlib.metadata, 'entry_points', lambda: EmptyEntryPoints())
    results = run_scheduled_check(run_checks_fn=updates.run_checks, plugins_dir=plug.parent,
                                 config_get=lambda *_: 24)
    assert results[0].update_available is None
    persisted = json.dumps(receipt.latest())
    cmd_check_updates(SimpleNamespace(json=True))
    output = capsys.readouterr().out
    assert {"result": marker in results[0].reason, "receipt": marker in persisted,
            "cli_json": marker in output} == {"result": False, "receipt": False, "cli_json": False}

    assert marker not in caplog.text
