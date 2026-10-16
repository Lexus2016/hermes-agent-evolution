"""Required product failures remain fatal after Node installs moved into PM.

The former node-deps stage is gone; exercise its source-completion replacement.
"""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[3]


@pytest.mark.platforms('posix')
@pytest.mark.parametrize('exit_code, diagnostic', [(37, 'simulated npm lifecycle failure'), (37, ''), (0, '')])
def test_product_completion_reports_failure(tmp_path: Path, exit_code: int, diagnostic: str) -> None:
    install = tmp_path / 'source tree'
    (install / 'hermes_cli').mkdir(parents=True)
    (install / 'hermes_cli' / 'source_completion.py').write_text(
        f'import sys\nprint({diagnostic!r}, file=sys.stderr)\nsys.exit({exit_code})\n', encoding='utf-8')
    command = ['bash', '-c',
               'source "$1" --manifest >/dev/null; INSTALL_DIR="$2"; INCLUDE_DESKTOP=false; '
               'bootstrap_python() { boot_py="$PYTHON_FOR_TEST"; }; stage_products',
               'test', str(ROOT / 'scripts/install.sh'), str(install)]
    result = subprocess.run(command, cwd=tmp_path,
                            env={**os.environ, 'HOME': str(tmp_path), 'HERMES_HOME': str(tmp_path / 'home'),
                                 'HERMES_RUNTIME_DIR': str(tmp_path / 'store'), 'PYTHON_FOR_TEST': sys.executable},
                            capture_output=True, text=True, timeout=20, check=False)
    output = result.stdout + result.stderr
    assert (result.returncode == 0) == (exit_code == 0), output
    if exit_code:
        assert 'app products or command publication failed' in output
        assert 'app products and hermes command ready' not in output
        if diagnostic:
            assert diagnostic in output
    else:
        assert 'app products and hermes command ready' in output


@pytest.mark.platforms('posix')
@pytest.mark.parametrize('origin', [
    'https://github.com/Lexus2016/hermes-agent-evolution',
    'git@github.com:Lexus2016/hermes-agent-evolution.git',
])
def test_source_redirect_covers_fork_and_upstream_aliases(tmp_path: Path, origin: str) -> None:
    repo = tmp_path / 'repo'
    repo.mkdir()
    work = tmp_path / 'work'
    work.mkdir()
    serve = tmp_path / 'serve.git'
    subprocess.run(['git', 'init', '-q', str(repo)], check=True)
    subprocess.run(['git', '-C', str(repo), 'remote', 'add', 'origin', origin], check=True)
    aliases = [base + suffix for base in (
        'https://github.com/NousResearch/hermes-agent',
        'git@github.com:NousResearch/hermes-agent',
        'https://github.com/Lexus2016/hermes-agent-evolution',
        'git@github.com:Lexus2016/hermes-agent-evolution',
    ) for suffix in ('', '.git')]
    command = ['bash', '-c',
               'set -eu; source "$1"; fail() { echo "$*" >&2; exit 1; }; ok() { :; }; '
               'arm_source_redirect "$2" "$3" "$4"; '
               '[ "$(git -C "$2" remote get-url origin)" = https://github.com/NousResearch/hermes-agent.git ]; '
               'repo="$2"; serve="$4"; shift 4; '
               'for url in "$@"; do '
               '"$HERMES_E2E_REAL_GIT" -C "$repo" remote set-url origin "$url"; '
               '[ "$("$HERMES_E2E_REAL_GIT" -C "$repo" remote get-url origin)" = "file://$serve" ]; done',
               'test', str(ROOT / 'tests/install/e2e-assets/installer-common.sh'), str(repo), str(work), str(serve), *aliases]
    result = subprocess.run(command, env={**os.environ, 'HOME': str(tmp_path), 'GIT_CONFIG_NOSYSTEM': '1'},
                            capture_output=True, text=True, timeout=20, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
