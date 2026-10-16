"""Exact-head independent receipts and real merge CLI admission (#3467)."""
import json
import copy
import sqlite3
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
from evolution_admission import VALIDATION_STEPS, record_admission, verify_receipt
import evolution_merge_gate as gate

NOW = datetime(2026, 10, 15, tzinfo=UTC)
HEAD = "a" * 40
SLUG = "owner/repo"


def receipts():
    run = dict(id=10, run_attempt=1, head_sha=HEAD, path=".github/workflows/ci.yaml",
               event="pull_request", repository={"full_name": SLUG}, status="completed",
               conclusion="success", updated_at=NOW.isoformat(),
               pull_requests=[{"number": 1, "base": {"sha": "base"}, "head": {"sha": HEAD}}])
    jobs = [dict(name=n, run_id=10, head_sha=HEAD, status="completed", conclusion="success")
            for n in ("All required checks pass (v2)",
                      "Python lints / Windows footguns (blocking)",
                      "Python lints / Agent Skills spec check (blocking)")]
    for job in jobs:
        job["steps"] = [{"name": VALIDATION_STEPS.get(job["name"], "aggregate"),
                         "status": "completed", "conclusion": "success"}]
    jobs.append(dict(name="Python tests / Run tests slice 1/1", run_id=10, head_sha=HEAD,
                     status="completed", conclusion="success", steps=[{
                         "name": "Run tests (slice 1/1)", "status": "completed", "conclusion": "success"}]))
    return run, jobs


def pr_binding():
    return {"number": 1, "state": "open", "head": {"sha": HEAD, "ref": "evolution/issue-1",
            "repo": {"full_name": SLUG}}, "base": {"sha": "base", "ref": "main",
            "repo": {"full_name": SLUG}}}


def dispatch_receipts(run, jobs):
    supplemental = dict(run, id=11, event="workflow_dispatch", pull_requests=[])
    e2e = [dict(name="Python tests / e2e", run_id=11, head_sha=HEAD,
                status="completed", conclusion="success", steps=[{
                    "name": "Run e2e tests", "status": "completed", "conclusion": "success"}])]
    return supplemental, e2e


def runner_for(run, jobs, *, supplemental=None, supplemental_jobs=None, current=None):
    default_run, default_jobs = dispatch_receipts(run, jobs)
    supplemental = default_run if supplemental is None else supplemental
    supplemental_jobs = default_jobs if supplemental_jobs is None else supplemental_jobs
    def runner(cmd):
        endpoint = cmd[-1]
        if endpoint.endswith("/pulls/1"):
            payload = current if current is not None else pr_binding()
        elif "/compare/" in endpoint:
            payload = {"files": [{"filename": "tools/example.py", "status": "modified"}]}
        elif "/runs/11/" in endpoint:
            payload = {"jobs": supplemental_jobs}
        elif "/jobs?" in endpoint:
            payload = {"jobs": jobs}
        elif "event=workflow_dispatch" in endpoint:
            payload = {"workflow_runs": [supplemental] if supplemental else []}
        else:
            payload = {"workflow_runs": [run]}
        return 0, json.dumps([payload]), ""
    return runner


def test_mixed_classes_require_union_and_exact_head():
    run, jobs = receipts()
    files = [{"path": "skills/x/SKILL.md"}, {"path": "agent/x.py"}, {"path": "plugins/x.py"}]
    record, errors = verify_receipt(files, HEAD, SLUG, runner_for(run, jobs), pr=1, now=NOW)
    assert not errors and set(record["classes"]) == {"memory-skill", "control", "plugin"}
    jobs.pop()
    assert verify_receipt(files, HEAD, SLUG, runner_for(run, jobs), pr=1, now=NOW)[1]


@pytest.mark.parametrize("mutation", [
    {"head_sha": "b" * 40}, {"path": ".github/workflows/self-grade.yaml"},
    {"event": "workflow_dispatch"}, {"conclusion": "failure"},
    {"repository": {"full_name": "attacker/fork"}}, {"updated_at": "2020-01-01T00:00:00Z"},
])
def test_untrusted_or_stale_provenance_fails_closed(mutation):
    run, jobs = receipts()
    run.update(mutation)
    assert verify_receipt([{"path": "tools/x.py"}], HEAD, SLUG, runner_for(run, jobs), pr=1, now=NOW)[1]


def test_workflow_change_and_self_grade_cannot_attest():
    run, jobs = receipts()
    assert verify_receipt([{"path": ".github/workflows/ci.yaml"}], HEAD, SLUG, runner_for(run, jobs), pr=1, now=NOW)[1]
    assert verify_receipt([{"path": "tools/x.py"}], HEAD, SLUG,
                          lambda cmd: (0, json.dumps([{"self_grade": "pass"}]), ""), pr=1, now=NOW)[1]
    jobs[0]["head_sha"] = "old"
    assert verify_receipt([{"path": "agent/x.py"}], HEAD, SLUG, runner_for(run, jobs), pr=1, now=NOW)[1]


def test_cycle_budget_is_atomic_idempotent_and_profile_scoped(tmp_path, monkeypatch):
    record = {"head": HEAD, "regressions": 1, "regression_budget": 1}
    store = tmp_path / "budget.sqlite3"
    with ThreadPoolExecutor(4) as pool:
        results = list(pool.map(lambda _: record_admission(record, repository=SLUG, cycle="cycle", store=store), range(8)))
    assert all(r["admitted_heads"] == 1 and r["regressions"] == 1 for r in results)
    with pytest.raises(ValueError, match="budget"):
        record_admission(dict(record, head="other"), repository=SLUG, cycle="cycle", store=store)
    for profile in ("A", "B", "A"):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / profile))
        assert record_admission(record, repository=SLUG, cycle="cycle")["admitted_heads"] == 1
    with sqlite3.connect(store) as db:
        assert db.execute("SELECT COUNT(*) FROM admissions").fetchone()[0] == 1


def test_cli_blocks_self_graded_only_evolution_change_before_merge(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    calls = []
    def runner(cmd):
        calls.append(cmd)
        if cmd[1:3] == ["pr", "view"]:
            return 0, json.dumps({"files": [{"path": "docs/note.md", "additions": 1}],
                                 "headRefOid": HEAD, "headRefName": "evolution/issue-3467"}), ""
        return 0, json.dumps([{"workflow_runs": []}]), ""
    monkeypatch.setattr(gate, "_run", runner)
    assert gate.main(["gate", "--pr", "1", "--repo", SLUG, "--merge"]) == 1
    assert not any("PUT" in c for c in calls)


def test_cli_verified_receipt_reaches_atomic_merge_and_records_cycle(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    run, jobs = receipts()
    run["updated_at"] = datetime.now(UTC).isoformat()
    calls = []
    def runner(cmd):
        calls.append(cmd)
        if cmd[1:3] == ["pr", "view"]:
            return 0, json.dumps({"files": [{"path": "tools/x.py", "additions": 1}],
                                 "headRefOid": HEAD, "headRefName": "evolution/issue-3467"}), ""
        if "PUT" in cmd:
            return 0, '{"merged": true}', ""
        return runner_for(run, jobs)(cmd)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(gate, "_run", runner)
    assert gate.main(["gate", "--pr", "1", "--repo", SLUG, "--merge"]) == 0
    assert any("PUT" in c and f"sha={HEAD}" in c for c in calls)
    with sqlite3.connect(tmp_path / "evolution" / "admission.sqlite3") as db:
        row = db.execute("SELECT cycle, receipt FROM admissions").fetchone()
    assert row[0] == "evolution/issue-3467" and json.loads(row[1])["run_id"] == run["id"]
    assert json.loads(row[1])["e2e_run_id"] == 11


def test_admission_enforcement_cannot_self_modify():
    assert any("HIGH_RISK_PATH" in error for error in gate.check_merge_policy(
        [{"path": "scripts/evolution_admission.py", "additions": 1}]))


@pytest.mark.parametrize("path,status", [("tests/test_old.py", "modified"),
    ("tests/fixture.json", "removed"), ("tests/conftest.py", "added"),
    ("scripts/run_tests_parallel.py", "modified")])
def test_candidate_cannot_rewrite_independent_validation(path, status):
    run, jobs = receipts()
    def runner(cmd):
        if "/compare/" in cmd[-1]:
            return 0, json.dumps([{"files": [{"filename": path, "status": status}]}]), ""
        return runner_for(run, jobs)(cmd)
    assert verify_receipt([{"path": "tools/x.py"}], HEAD, SLUG, runner, pr=1, now=NOW)[1]


def test_skipped_test_step_cannot_be_hidden_by_successful_job():
    run, jobs = receipts()
    jobs[-1]["steps"][0]["conclusion"] = "skipped"
    assert verify_receipt([{"path": "tools/x.py"}], HEAD, SLUG, runner_for(run, jobs), pr=1, now=NOW)[1]


def test_pr_slices_and_dispatched_e2e_are_distinct_required_receipts():
    run, jobs = receipts()
    supplemental, e2e = dispatch_receipts(run, jobs)
    record, errors = verify_receipt([{"path": "tools/x.py"}], HEAD, SLUG,
        runner_for(run, jobs, supplemental=supplemental, supplemental_jobs=e2e), pr=1, now=NOW)
    assert not errors
    assert record["run_id"] == 10 and record["e2e_run_id"] == 11
    assert record["baseline"] == "base" and record["pr"] == 1
    # An E2E receipt cannot stand in for the PR's actually executed baseline suite.
    assert verify_receipt([{"path": "tools/x.py"}], HEAD, SLUG,
        runner_for(run, jobs[:-1], supplemental=supplemental, supplemental_jobs=e2e), pr=1, now=NOW)[1]


@pytest.mark.parametrize("mutation", ["missing", "wrong_head", "wrong_repo", "wrong_event", "wrong_path",
    "stale", "future", "in_progress", "failed", "missing_step", "skipped_step", "job_head", "job_run"])
def test_supplemental_receipt_failures_give_safe_nonrelease_recovery(mutation):
    run, jobs = receipts()
    supplemental, e2e = dispatch_receipts(run, jobs)
    changes = {
        "wrong_head": {"head_sha": "other"}, "wrong_repo": {"repository": {"full_name": "other/repo"}},
        "wrong_event": {"event": "push"}, "wrong_path": {"path": ".github/workflows/other.yml"},
        "stale": {"updated_at": "2020-01-01T00:00:00Z"}, "future": {"updated_at": "2030-01-01T00:00:00Z"},
        "in_progress": {"status": "in_progress"}, "failed": {"conclusion": "failure"},
    }
    supplemental.update(changes.get(mutation, {}))
    if mutation == "missing":
        supplemental = {}
    if mutation == "missing_step":
        e2e[0]["steps"] = []
    if mutation == "skipped_step":
        e2e[0]["steps"][0]["conclusion"] = "skipped"
    if mutation == "job_head":
        e2e[0]["head_sha"] = "other"
    if mutation == "job_run":
        e2e[0]["run_id"] = 10
    calls = []
    runner = runner_for(run, jobs, supplemental=supplemental, supplemental_jobs=e2e)
    def observed(cmd):
        calls.append(cmd)
        return runner(cmd)
    _, errors = verify_receipt([{"path": "tools/x.py"}], HEAD, SLUG, observed, pr=1, now=NOW)
    assert errors and "release=false" in errors[0] and HEAD in errors[0]
    assert "--ref evolution/issue-1" in errors[0]
    assert all(cmd[1] == "api" and "--method" not in cmd for cmd in calls)


@pytest.mark.parametrize("field,value", [("head", "new-head"), ("base", "new-base")])
def test_changed_pr_binding_prevents_receipt_and_real_merge(field, value, monkeypatch, tmp_path):
    run, jobs = receipts()
    run["updated_at"] = datetime.now(UTC).isoformat()
    changed = pr_binding()
    changed[field]["sha"] = value
    assert verify_receipt([{"path": "tools/x.py"}], HEAD, SLUG,
                         runner_for(run, jobs, current=changed), pr=1)[1]
    good = runner_for(run, jobs)
    calls, bindings = [], 0
    def runner(cmd):
        nonlocal bindings
        calls.append(cmd)
        if cmd[1:3] == ["pr", "view"]:
            return 0, json.dumps({"files": [{"path": "tools/x.py", "additions": 1}],
                                 "headRefOid": HEAD, "headRefName": "evolution/issue-1"}), ""
        if cmd[-1].endswith("/pulls/1"):
            bindings += 1
            if bindings == 2:
                return 0, json.dumps([changed]), ""
        return good(cmd)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    monkeypatch.setattr(gate, "_run", runner)
    assert gate.main(["gate", "--pr", "1", "--repo", SLUG, "--merge"]) == 1
    assert bindings == 2 and not any("PUT" in cmd for cmd in calls)
    assert not (tmp_path / ".hermes" / "evolution" / "admission.sqlite3").exists()


def test_latest_supplemental_attempt_cannot_reuse_older_success():
    run, jobs = receipts()
    supplemental, e2e = dispatch_receipts(run, jobs)
    runner = runner_for(run, jobs, supplemental=supplemental, supplemental_jobs=e2e)
    calls = []
    def latest(cmd):
        calls.append(cmd[-1])
        if "event=workflow_dispatch" in cmd[-1]:
            return 0, json.dumps([{"workflow_runs": [supplemental, dict(supplemental, run_attempt=2)]}]), ""
        if "/runs/11/attempts/2/" in cmd[-1]:
            failed = copy.deepcopy(e2e)
            failed[0]["steps"][0]["conclusion"] = "failure"
            return 0, json.dumps([{"jobs": failed}]), ""
        return runner(cmd)
    assert verify_receipt([{"path": "tools/x.py"}], HEAD, SLUG, latest, pr=1, now=NOW)[1]
    assert any("/runs/11/attempts/2/jobs" in endpoint for endpoint in calls)
    assert not any("/runs/11/attempts/1/jobs" in endpoint for endpoint in calls)


@pytest.mark.parametrize("change", [
    {"filename": ".github/workflows/ci.yaml", "status": "modified"},
    {"filename": "notes/old-ci.txt", "previous_filename": ".github/workflows/ci.yaml", "status": "renamed"},
    {"filename": ".github/actions/setup-pm/action.yml", "status": "modified"},
])
def test_supplemental_cannot_attest_to_modified_or_renamed_validation(change):
    run, jobs = receipts()
    good = runner_for(run, jobs)
    def runner(cmd):
        if "/compare/" in cmd[-1]:
            return 0, json.dumps([{"files": [change]}]), ""
        return good(cmd)
    _, errors = verify_receipt([{"path": change["filename"]}], HEAD, SLUG, runner, pr=1, now=NOW)
    assert errors


def test_older_success_cannot_hide_newer_failed_supplemental_run():
    run, jobs = receipts()
    supplemental, e2e = dispatch_receipts(run, jobs)
    good = runner_for(run, jobs, supplemental=supplemental, supplemental_jobs=e2e)
    def runner(cmd):
        if "event=workflow_dispatch" in cmd[-1]:
            return 0, json.dumps([{"workflow_runs": [supplemental,
                dict(supplemental, id=12, conclusion="failure")]}]), ""
        return good(cmd)
    assert verify_receipt([{"path": "tools/x.py"}], HEAD, SLUG, runner, pr=1, now=NOW)[1]


def test_valid_same_head_revalidation_refreshes_baseline_receipts_without_double_charge(tmp_path):
    store = tmp_path / "admission.sqlite3"
    run, jobs = receipts()
    record, errors = verify_receipt([{"path": "tools/x.py"}], HEAD, SLUG,
                                   runner_for(run, jobs), pr=1, now=NOW)
    assert not errors
    first = record_admission(record, repository=SLUG, cycle="cycle", store=store)
    current = pr_binding()
    current["base"]["sha"] = "new-base"
    run["pull_requests"][0]["base"]["sha"] = "new-base"
    run["id"] = 12
    for job in jobs:
        job["run_id"] = 12
    refreshed, errors = verify_receipt([{"path": "tools/x.py"}], HEAD, SLUG,
                                      runner_for(run, jobs, current=current), pr=1, now=NOW)
    assert not errors
    second = record_admission(refreshed, repository=SLUG, cycle="cycle", store=store)
    assert first == second and second["regressions"] == 0 and second["admitted_heads"] == 1
    with sqlite3.connect(store) as db:
        rows = db.execute("SELECT receipt FROM admissions").fetchall()
    assert len(rows) == 1 and json.loads(rows[0][0]) == refreshed
    assert refreshed["baseline"] == "new-base" and refreshed["run_id"] == 12
    with pytest.raises(ValueError, match="regression count"):
        record_admission(dict(refreshed, regressions=1, regression_budget=1),
                         repository=SLUG, cycle="cycle", store=store)


def test_receipts_follow_actual_workflow_graph_and_executed_step_contracts():
    import hermes_yaml as yaml
    from tests.ci import _gha_expr as gha
    from tests.ci import workflow_steps

    repo = Path(__file__).resolve().parents[2]
    def workflow(name):
        return yaml.safe_load((repo / '.github/workflows' / name).read_text(encoding='utf-8-sig'))
    ci, tests, lint = workflow('ci.yaml'), workflow('tests.yml'), workflow('lint.yml')
    run, jobs = receipts()
    jobs[0]['name'] = ci['jobs']['all-checks-pass']['name']
    for job, key in zip(jobs[1:3], ('windows-footguns', 'skills-spec')):
        definition = lint['jobs'][key]
        job['name'] = ci['jobs']['lint']['name'] + ' / ' + definition['name']
        job['steps'] = [dict(name=step['name'], status='completed', conclusion='success')
                        for step in definition['steps'] if 'name' in step]
    slice_ctx = {'matrix': {'slice': {'index': 1}}, 'inputs': {'slice_count': 1}}
    definition = tests['jobs']['test']
    jobs[-1]['name'] = ci['jobs']['tests']['name'] + ' / ' + gha.render(definition['name'], slice_ctx)
    jobs[-1]['steps'] = [dict(name=gha.render(step['name'], slice_ctx), status='completed', conclusion='success')
                        for step in definition['steps'] if 'name' in step]

    # Execute the real routing step: the PR omits E2E, a non-release dispatch enables it.
    detect = ci['jobs']['detect']
    gate_step = next(step for step in detect['steps'] if step.get('id') == 'gate-lanes')
    e2e_enabled = {}
    for event in ('pull_request', 'workflow_dispatch'):
        ctx = {'inputs': {'release': False}, 'github': {'repository': SLUG, 'event_name': event, 'event': {}},
               'steps': {'classify': {'outputs': {'python': 'true', 'e2e': 'true', 'e2e_upgrade': 'false'}}}}
        ctx['steps']['gate-lanes'] = {'outputs': workflow_steps.outputs(gate_step, ctx)}
        outputs = {name: gha.render(value, ctx) for name, value in detect['outputs'].items()}
        ctx['needs'] = {'detect': {'outputs': outputs}}
        assert gha.condition(ci['jobs']['tests']['if'], ctx)
        called_inputs = {name: gha.render(value, ctx) for name, value in ci['jobs']['tests']['with'].items()}
        e2e_enabled[event] = gha.condition(tests['jobs']['e2e']['if'], {'inputs': called_inputs})
    assert e2e_enabled == {'pull_request': False, 'workflow_dispatch': True}

    supplemental, e2e = dispatch_receipts(run, jobs)
    e2e[0]['name'] = ci['jobs']['tests']['name'] + ' / ' + tests['jobs']['e2e'].get('name', 'e2e')
    e2e[0]['steps'] = [dict(name=step['name'], status='completed', conclusion='success')
                      for step in tests['jobs']['e2e']['steps'] if 'name' in step]
    files = [{'path': 'agent/example.py'}, {'path': 'skills/example/SKILL.md'}]
    runner = runner_for(run, jobs, supplemental=supplemental, supplemental_jobs=e2e)
    record, errors = verify_receipt(files, HEAD, SLUG, runner, pr=1, now=NOW)
    assert not errors and record['e2e_run_id'] == supplemental['id']
    for step in e2e[0]['steps']:
        if step['name'] == 'Run e2e tests':
            step['conclusion'] = 'skipped'
    assert verify_receipt(files, HEAD, SLUG, runner, pr=1, now=NOW)[1]
