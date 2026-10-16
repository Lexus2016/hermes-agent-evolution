"""Fresh owner-side decision checks fence queued cron work and writer races."""
import asyncio
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from cron import jobs, scheduler
from gateway import run, session_cron, session_runtime_workers
from gateway.config import GatewayConfig, Platform
from gateway.session import SessionStore
from gateway.session_authority import SessionAuthority
from gateway.session_contract import SessionRef
from gateway.session_local_recovery import local_adapter_map
from hermes_state import SessionDB
from hermes_state_runtime import (
    RuntimeStoreError, begin_runtime_epoch, claim_session_input, list_session_admissions,
)


@pytest.fixture
def owner(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setattr(run, '_load_gateway_config', lambda: {'model': {}, 'platform_toolsets': {'cli': []}})
    with jobs.use_cron_store(tmp_path / 'cron'):
        evidence = tmp_path / 'premise.txt'
        evidence.write_text('ready')
        job = jobs.create_job('report', 'every 1h', deliver='local', model='fixture',
                              decision={'watch_files': [str(evidence)]})
        db = SessionDB(tmp_path / 'state.db')
        runner = SimpleNamespace(_draining=False, adapters={},
                                 session_store=SessionStore(tmp_path / 'sessions', GatewayConfig()))
        authority = SessionAuthority(runner, profile_id=str(tmp_path), instance_id='test', db=db,
                                     epoch=begin_runtime_epoch(db, instance_id='test'))
        runner.session_authority = authority
        monkeypatch.setattr(authority, '_schedule', lambda ref: None)
        try:
            yield authority, job, evidence
        finally:
            db.close()


@pytest.mark.parametrize('writer_call', [1, 2])
def test_replan_during_submit_writer_wait_never_admits_old_decision(owner, monkeypatch, writer_call):
    authority, job, evidence = owner
    original = session_runtime_workers.tracked_write
    count = 0
    newer = None

    async def replan_before_write(*args, **kwargs):
        nonlocal count, newer
        count += 1
        if count == writer_call:
            await asyncio.sleep(0)
            newer = jobs.update_job(job['id'], {'decision': {'watch_files': [str(evidence)]}})
        return await original(*args, **kwargs)

    monkeypatch.setattr(session_runtime_workers, 'tracked_write', replan_before_write)
    with pytest.raises(RuntimeStoreError, match='admission_conflict'):
        asyncio.run(session_cron.operation(authority, 'submit', {
            'job_id': job['id'], 'request_id': 'fire', 'extra_prompt': None,
        }))
    assert newer is not None
    assert jobs.get_job(job['id'])['decision_ledger'] == newer['decision_ledger']
    for sid in authority.sessions:
        assert list_session_admissions(authority.db, session_id=sid, pending_only=False) == []


@pytest.mark.parametrize('change', ['replan', 'evidence'])
def test_owner_execution_rechecks_after_queue_wait_before_running_job(owner, monkeypatch, change):
    authority, job, evidence = owner
    run_job = Mock(side_effect=AssertionError('stale plan reached scripts/monitors/model'))
    monkeypatch.setattr(scheduler, 'run_job', run_job)
    original = session_cron._run_identified
    current = {}

    def change_after_dispatch(*args, **kwargs):
        if change == 'replan':
            current.update(jobs.update_job(job['id'], {'decision': {'watch_files': [str(evidence)]}}))
        else:
            evidence.write_text('revoked')
        return original(*args, **kwargs)

    monkeypatch.setattr(session_cron, '_run_identified', change_after_dispatch)

    async def probe():
        receipt = await session_cron.operation(authority, 'submit', {
            'job_id': job['id'], 'request_id': 'fire', 'extra_prompt': None,
        })
        sid = receipt['session_id']
        row = claim_session_input(authority.db, epoch=authority.epoch, session_id=sid)
        policy = local_adapter_map(authority)[Platform.LOCAL].policies[sid]
        with pytest.raises(RuntimeError):
            await session_cron.execute(authority, SessionRef(authority.profile_id, sid), row, policy)
        result = authority.pending_results[row['admission_id']]['result']['cron_result']
        assert result[0] is False and result[3]

    asyncio.run(probe())
    run_job.assert_not_called()
    stored = jobs.get_job(job['id'])
    if change == 'replan':
        assert stored['decision_ledger'] == current['decision_ledger']
        assert stored['enabled'] is True
    else:
        assert stored['decision_ledger']['state'] == 'reopened'
        assert stored['enabled'] is False


def test_decision_fences_use_owner_profile_not_ambient_launch_home(owner, monkeypatch):
    from pathlib import Path
    from cron import decision_ledger
    from hermes_constants import get_hermes_home

    authority, job, _evidence = owner
    owner_home = Path(authority.db.db_path).parent
    launch_home = owner_home / 'other-launch-home'
    launch_home.mkdir()
    monkeypatch.setenv('HERMES_HOME', str(launch_home))
    authority.runner.config = SimpleNamespace(multiplex_profiles=True)
    real_preflight = decision_ledger.preflight_current
    scopes = []

    def observe_scope(snapshot, *, persist=True):
        scopes.append((Path(get_hermes_home()), persist))
        return real_preflight(snapshot, persist=persist)

    monkeypatch.setattr(decision_ledger, 'preflight_current', observe_scope)
    monkeypatch.setattr(scheduler, 'run_job', lambda *a, **kw: (True, '', 'done', None))

    async def probe():
        receipt = await session_cron.operation(authority, 'submit', {
            'job_id': job['id'], 'request_id': 'scoped-fire', 'extra_prompt': None,
        })
        sid = receipt['session_id']
        row = claim_session_input(authority.db, epoch=authority.epoch, session_id=sid)
        policy = local_adapter_map(authority)[Platform.LOCAL].policies[sid]
        assert await session_cron.execute(authority, SessionRef(authority.profile_id, sid), row, policy) == 'done'

    asyncio.run(probe())
    assert scopes == [(owner_home, False), (owner_home, True)]
    assert Path(get_hermes_home()) == launch_home
