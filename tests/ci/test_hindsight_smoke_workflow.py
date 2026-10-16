"""The installed Hindsight smoke is a required, read-only Linux CI consumer."""
from pathlib import Path

import hermes_yaml as yaml
import pytest

from scripts.ci.required_results import evaluate_gate

ROOT = Path(__file__).resolve().parents[2]


def test_real_hindsight_smoke_is_required_and_pinned_to_candidate():
    tests = yaml.safe_load((ROOT / '.github/workflows/tests.yml').read_text(encoding='utf-8-sig'))
    ci = yaml.safe_load((ROOT / '.github/workflows/ci.yaml').read_text(encoding='utf-8-sig'))
    job = tests['jobs']['hindsight-smoke']
    assert job['runs-on'] == 'ubuntu-latest'
    assert job['timeout-minutes'] == 40
    assert job['permissions'] == {'contents': 'read'}
    assert 'if' not in job
    assert not job.get('continue-on-error')
    checkout, setup, smoke, clean = job['steps']
    assert checkout['with']['ref'] == '${{ inputs.commit }}'
    assert setup['uses'] == './.github/actions/setup-pm'
    assert setup['with']['extras'] == '["hindsight"]'
    assert '"$HERMES_PYTHON" scripts/smoke_hindsight_isolated.py' in smoke['run']
    assert '--work-root "$RUNNER_TEMP/hermes-hindsight-smoke"' in smoke['run']
    assert 'if' not in smoke and not smoke.get('continue-on-error')
    assert clean['uses'] == './.github/actions/check-clean-worktree'
    assert ci['jobs']['tests']['uses'] == './.github/workflows/tests.yml'
    assert 'tests' in ci['jobs']['all-checks-pass']['needs']
    assert not ci['jobs']['tests'].get('continue-on-error')


@pytest.mark.parametrize('result,ok', [
    ('success', True), ('failure', False), ('cancelled', False), ('skipped', False), (None, False),
])
def test_selected_hindsight_consumer_is_required_by_real_aggregate(result, ok):
    ci = yaml.safe_load((ROOT / '.github/workflows/ci.yaml').read_text(encoding='utf-8-sig'))
    needs = {name: {'result': 'success'} for name in ci['jobs']['all-checks-pass']['needs']}
    needs['detect']['outputs'] = {
        'python': 'true', 'e2e': 'false', 'e2e_upgrade': 'false', 'tests_os': 'false',
    }
    # The unconditional smoke is inside the tests reusable-workflow consumer.
    # A missing/skipped required consumer must fail even on an ordinary PR.
    if result is None:
        del needs['tests']
    else:
        needs['tests']['result'] = result
    verdict = evaluate_gate(needs)
    assert verdict['ok'] is ok
    if not ok:
        assert 'tests' in verdict['failed']
