"""Native security jobs retain their named proof contracts on the locked toolchain."""

import ast
import hashlib
import json
from pathlib import Path

import hermes_yaml as yaml
import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize('filename,job_name,branch', [
    ('media-cache-security-native.yml', 'native-security', 'media-cache'),
    ('process-lifetime-native.yml', 'native-process', 'process-lifetime'),
])
def test_native_security_uses_exact_candidate_and_canonical_test_environment(filename, job_name, branch):
    workflow = yaml.safe_load((ROOT / '.github/workflows' / filename).read_text(encoding='utf-8'))
    assert workflow['permissions'] == {'contents': 'read'}
    triggers = workflow.get('on', workflow.get(True))
    assert set(triggers) == {'push', 'workflow_dispatch'}
    assert triggers['push']['branches'] == [f'security-validation/{branch}-*']
    job = workflow['jobs'][job_name]
    assert job['timeout-minutes'] == 30
    assert job['strategy']['matrix']['include'] == [
        {'runner': 'macos-latest', 'platform': 'macos'},
        {'runner': 'windows-latest', 'platform': 'windows'},
    ]
    checkout, identity, setup, execute = job['steps']
    assert checkout['with'] == {'ref': '${{ github.sha }}', 'persist-credentials': False}
    assert 'git rev-parse HEAD' in identity['run']
    assert identity['env']['EXPECTED_SHA'] == '${{ github.sha }}'
    assert setup['uses'] == './.github/actions/setup-pm'
    assert setup['with']['test-environment'] == 'true'
    for cache in ('cache', 'cache-python', 'cache-node'):
        assert setup['with'][cache] == 'false'
    assert execute['env']['HERMES_TEST_FILE_RETRIES'] == '0'
    script = execute['run']
    assert 'bash scripts/run_tests.sh' in script
    assert '"$HERMES_PYTHON" -' in script
    assert 'uv run' not in script and 'macos_only' not in script and 'windows_only' not in script
    assert '-m "$NATIVE_PLATFORM"' not in script
    assert '("failure", "error", "skipped")' in script
    assert 'missing executed family' in script
    for step in job['steps']:
        assert not step.get('continue-on-error')
        assert 'if' not in step


def test_native_media_selects_retained_real_health_and_policy_families():
    workflow = yaml.safe_load((ROOT / '.github/workflows/media-cache-security-native.yml').read_text(encoding='utf-8'))
    script = workflow['jobs']['native-security']['steps'][-1]['run']
    assert '-k "test_no_name_error_when_json_always_fails or test_connect_keeps_real_health_checks_and_private_bridge_cache"' in script
    assert '-k test_non_posix_exclusive_write_is_supported' in script
    for policy in ('private', 'docker_uid_gid', 'container_preserved', 'container_private',
                   'container_invalid', 'skip_chmod_preserved', 'skip_chmod_private',
                   'skip_chmod_invalid', 'detected_preserved', 'detected_private', 'detected_invalid'):
        for kind in ('image', 'document'):
            assert f'test_explicit_policy_preserves_private_payload_contract_macos[{policy}-{kind}]' in script
    names = {node.name for node in ast.walk(ast.parse(
        (ROOT / 'tests/gateway/test_whatsapp_connect.py').read_text(encoding='utf-8')))
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
    assert 'test_connect_keeps_real_health_checks_and_private_bridge_cache' in names


def _native_receipt_program(filename):
    workflow = yaml.safe_load((ROOT / '.github/workflows' / filename).read_text(encoding='utf-8'))
    job = next(iter(workflow['jobs'].values()))
    return job['steps'][-1]['run'].split('"$HERMES_PYTHON" - <<\'PY\'\n', 1)[1].rsplit('\nPY', 1)[0]


@pytest.mark.parametrize('filename', ['media-cache-security-native.yml', 'process-lifetime-native.yml'])
@pytest.mark.parametrize('platform', ['macos', 'windows'])
@pytest.mark.parametrize('condition', ['success', 'empty', 'missing', 'skipped', 'error', 'failure'])
def test_actual_native_receipt_parser_refuses_incomplete_proof(filename, platform, condition, tmp_path, monkeypatch):
    import xml.etree.ElementTree as ET

    monkeypatch.setenv('NATIVE_PLATFORM', platform)
    monkeypatch.setenv('SECURITY_RESULTS', str(tmp_path))
    monkeypatch.setenv('PROCESS_RESULTS', str(tmp_path))
    program = _native_receipt_program(filename)
    setup = program.split('for label, families in required.items():', 1)[0]
    namespace = {}
    exec(compile(setup, filename, 'exec'), namespace)
    required = namespace['required']
    # Reviewed main's proof inventories plus the retained real WhatsApp health
    # test. The original unit test remains required. Changes require review.
    inventory_hashes = {
        ('media-cache-security-native.yml', 'macos'): '62636dc7ea42a22add26a72def3b545d4ea86517eeed709dca5527b979429816',
        ('media-cache-security-native.yml', 'windows'): 'baf659b20a40f76ba50b35dbc910b9d31d9eba8fa146c240178ae9c63c6431aa',
        ('process-lifetime-native.yml', 'macos'): '94c2275937fea2660c710cd654b2db3843e69ce7a0e74622b48c1b36a7e257fb',
        ('process-lifetime-native.yml', 'windows'): 'c7245877996f3d8ea9db0f4e2de50d44babace818978a7d748d6c8fed5b637e5',
    }
    assert hashlib.sha256(json.dumps(required, sort_keys=True).encode()).hexdigest() == inventory_hashes[filename, platform]
    for label, families in required.items():
        suite = ET.Element('testsuite')
        for family in families or ['actual_test']:
            ET.SubElement(suite, 'testcase', name=family)
        ET.ElementTree(suite).write(tmp_path / f'{label}.xml', encoding='utf-8')
    first = tmp_path / f'{next(iter(required))}.xml'
    suite = ET.parse(first).getroot()
    if condition == 'empty':
        suite.clear()
    elif condition == 'missing':
        for case in suite:
            case.set('name', 'unrelated_test')
    elif condition != 'success':
        ET.SubElement(suite[0], condition)
    ET.ElementTree(suite).write(first, encoding='utf-8')
    if condition == 'success':
        exec(compile(program, filename, 'exec'), {})
    else:
        with pytest.raises(AssertionError):
            exec(compile(program, filename, 'exec'), {})


def test_actual_aggregate_rejects_cancelled_classifier_with_skipped_consumers():
    from scripts.ci.required_results import evaluate_gate

    workflow = yaml.safe_load((ROOT / '.github/workflows/ci.yaml').read_text(encoding='utf-8'))
    needs = {name: {'result': 'skipped'} for name in workflow['jobs']['all-checks-pass']['needs']}
    needs['detect'] = {'result': 'cancelled', 'outputs': {}}
    verdict = evaluate_gate(needs)
    assert not verdict['ok']
    assert 'detect' in verdict['failed']
