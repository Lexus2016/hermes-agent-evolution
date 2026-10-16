"""Read-only investigation: actual discovery, result, cadence and receipt boundaries."""
import json
from pathlib import Path
from types import SimpleNamespace
import pytest

@pytest.mark.parametrize('failure', ['feed', 'git', 'metadata'])
def test_unknown_update_is_visible_and_never_applied(tmp_path, monkeypatch, failure):
    from hermes_cli.plugins_updates import run_checks
    from hermes_cli.plugins_cadence import run_scheduled_check
    from pm import receipt
    home = tmp_path / '.hermes'
    home.mkdir()
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.setenv('HERMES_HOME', str(home))
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    plugins = home / 'plugins'
    plugins.mkdir()
    rows = {}
    for name in ['broken', 'healthy']:
        directory = plugins / name
        (directory / '.git').mkdir(parents=True)
        text = f'name: {name}\nversion: 1.0.0\n'
        rows[name] = {'source': 'https://example.invalid/' + name, 'revision': 'a' * 40}
        if failure == 'feed' and name == 'broken':
            rows[name]['update_url'] = 'https://example.invalid/feed.yml'
            text += 'update_url: https://example.invalid/feed.yml\n'
        (directory / 'plugin.yaml').write_text(text, encoding="utf-8")
    if failure == 'metadata':
        rows.pop('broken')
        (plugins / 'broken' / '.git').rmdir()
    (plugins / '.install-metadata.json').write_text(json.dumps(rows), encoding="utf-8")
    original = {str(p.relative_to(plugins)): p.read_bytes() for p in plugins.rglob('*') if p.is_file()}
    called = []
    def fetch(url):
        called.append(('fetch', url))
        raise OSError('synthetic fetch unavailable')
    def remote(source):
        called.append(('git', source))
        if source.endswith('/broken'):
            raise OSError('synthetic git unavailable')
        return 'b' * 40
    class BadDistribution:
        @property
        def metadata(self):
            raise ValueError('synthetic metadata unreadable')
    eps = [SimpleNamespace(name='bad-pip', dist=BadDistribution())] if failure == 'metadata' else []
    def no_pypi(name):
        pytest.fail('metadata failure must not guess a package name or access PyPI')
    def checks(directory):
        return run_checks(directory, fetch=fetch, ls_remote=remote, pip_entry_points=eps,
                          pip_installed_version=no_pypi, pip_pypi_latest=no_pypi)
    applied = []
    results = run_scheduled_check(run_checks_fn=checks, plugins_dir=plugins,
        apply_updates_fn=applied.append, config_get=lambda section,key: True if key == 'auto_apply' else 24)
    name = 'bad-pip' if failure == 'metadata' else 'broken'
    failed = next(row for row in results if row.name == name)
    assert failed.update_available is None
    assert failed.latest is None
    assert ('owning distribution metadata' if failure == 'metadata' else 'failed:') in failed.reason
    assert applied == ['healthy']
    assert original == {str(p.relative_to(plugins)): p.read_bytes() for p in plugins.rglob('*') if p.is_file()}
    persisted = receipt.latest()
    assert persisted['kind'] == 'plugin-check'
    assert persisted['outcome'] == 'updates-available'
    assert next(row for row in persisted['plugin_checks'] if row['name'] == name) == failed.to_json()
    assert (home / 'plugin-update-checks' / 'last-run').is_file()
    assert run_scheduled_check(run_checks_fn=lambda _: pytest.fail('rate limited'), plugins_dir=plugins,
        config_get=lambda *_: 24) is None
