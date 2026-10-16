"""Class-specific admission using GitHub-owned CI receipts, never self-grades (#3467)."""
from __future__ import annotations

import json
import re
import sqlite3
import shlex
from datetime import UTC, datetime
from pathlib import Path

# All applicable policies apply to mixed changes. Parity is required; gains cannot
# buy regressions. GRASP's baseline-expanded budget is intentionally not used.
CLASS_GATES = {
    "memory-skill": ("All required checks pass (v2)", "Python lints / Agent Skills spec check (blocking)"),
    "tool": ("All required checks pass (v2)", "Python tests / e2e"),
    "control": ("All required checks pass (v2)", "Python tests / e2e", "Python lints / Windows footguns (blocking)"),
    "plugin": ("All required checks pass (v2)", "Python tests / e2e"),
}
VALIDATION_STEPS = {
    "Python tests / e2e": "Run e2e tests",
    "Python lints / Windows footguns (blocking)": "Run footgun checker",
    "Python lints / Agent Skills spec check (blocking)": "Run skills spec check",
}
MAX_EVIDENCE_AGE_SECONDS = 7 * 24 * 60 * 60


def change_classes(files):
    classes = set()
    for item in files:
        path = item.get("path", "")
        if path.startswith(("tests/", "website/")):
            continue
        if path.startswith(("skills/", "optional-skills/")) or path.endswith(".md"):
            classes.add("memory-skill")
        elif path.startswith("plugins/"):
            classes.add("plugin")
        elif path.startswith("tools/"):
            classes.add("tool")
        else:
            classes.add("control")
    return sorted(classes or {"memory-skill"})


def _api(runner, endpoint):
    code, out, _ = runner(["gh", "api", "--paginate", "--slurp", endpoint])
    if code:
        raise ValueError("GitHub validation receipt unavailable")
    pages = json.loads(out)
    if not isinstance(pages, list) or not all(isinstance(p, dict) for p in pages):
        raise ValueError("Malformed GitHub validation receipt")
    return pages


def _baseline_evidence(run, head, slug, runner, pr):
    prs = run.get("pull_requests", [])
    bases = {p.get("base", {}).get("sha") for p in prs if p.get("head", {}).get("sha") == head and p.get("number") == pr}
    if len(bases) != 1 or not next(iter(bases)):
        raise ValueError("Missing unambiguous immutable baseline for independent validation data")
    base = next(iter(bases))
    pages = _api(runner, f"repos/{slug}/compare/{base}...{head}?per_page=100")
    changed = [f for p in pages for f in p.get("files", [])]
    if not changed or len(changed) >= 300:
        raise ValueError("Baseline comparison missing or at GitHub's file truncation limit")
    for item in changed:
        path = item.get("filename", "")
        old = item.get("previous_filename", path)
        baseline_test = old.startswith(("tests/", "evals/")) and item.get("status") != "added"
        machinery = any(
            name.startswith((".github/", "scripts/ci/", "scripts/run_tests", "scripts/check", "scripts/code_health/", "analysis/sync-health/"))
            or Path(name).name in {"conftest.py", "pytest.ini", "setup.cfg", "sitecustomize.py", "usercustomize.py", "ruff.toml", ".npmrc"}
            for name in (path, old)
        )
        if baseline_test or machinery:
            raise ValueError(f"Independent baseline validation changed: {path}; requires human review")
    return base


def _passed(job, head, run_id):
    return (job.get("head_sha") == head and job.get("run_id") == run_id
            and job.get("status") == "completed" and job.get("conclusion") == "success")


def _bound_pr(pr, head, baseline, slug, runner):
    pages = _api(runner, f"repos/{slug}/pulls/{pr}")
    if len(pages) != 1:
        raise ValueError("Missing unique PR binding")
    current = pages[0]
    if (current.get("number") != pr or current.get("state") != "open"
            or current.get("head", {}).get("sha") != head
            or current.get("head", {}).get("repo", {}).get("full_name") != slug
            or current.get("base", {}).get("sha") != baseline
            or current.get("base", {}).get("ref") != "main"
            or current.get("base", {}).get("repo", {}).get("full_name") != slug):
        raise ValueError("PR head/base/repository changed or is not the expected open fork PR")
    return current


def recheck_pr_binding(record, slug, runner):
    """Recheck before merge; GitHub's merge API only offers HEAD, not base, CAS."""
    try:
        _bound_pr(record["pr"], record["head"], record["baseline"], slug, runner)
        return []
    except (ValueError, TypeError, KeyError, OverflowError) as exc:
        return [f"ADMISSION_EVIDENCE: {exc}"]


def _trusted_run(head, slug, runner, event, now):
    pages = _api(runner, f"repos/{slug}/actions/workflows/ci.yaml/runs?head_sha={head}&event={event}&per_page=100")
    runs = [r for p in pages for r in p.get("workflow_runs", []) if r.get("head_sha") == head]
    if not runs:
        raise ValueError(f"Missing independent {event} CI workflow receipt for candidate head")
    run = max(runs, key=lambda r: (int(r.get("id", 0)), int(r.get("run_attempt", 0))))
    if (run.get("path") != ".github/workflows/ci.yaml" or run.get("event") != event
            or run.get("repository", {}).get("full_name") != slug
            or run.get("status") != "completed" or run.get("conclusion") != "success"):
        raise ValueError(f"Latest {event} CI run is not a completed successful trusted workflow")
    completed = datetime.fromisoformat(run["updated_at"])
    age = (now - completed).total_seconds()
    if not 0 <= age <= MAX_EVIDENCE_AGE_SECONDS:
        raise ValueError("CI evidence is stale or future-dated; rerun independent validation")
    run_id, attempt = int(run["id"]), int(run["run_attempt"])
    pages = _api(runner, f"repos/{slug}/actions/runs/{run_id}/attempts/{attempt}/jobs?per_page=100")
    jobs = [j for p in pages for j in p.get("jobs", [])]
    return run, jobs


def _step_passed(job, step):
    return any(s.get("name") == step and s.get("status") == "completed"
               and s.get("conclusion") == "success" for s in job.get("steps", []))


def _dispatch_instruction(slug, branch, head):
    command = shlex.join(["gh", "workflow", "run", "ci.yaml", "--repo", slug,
                          "--ref", branch, "-f", "release=false"])
    return (f"Supplemental E2E validation required. With existing validation authorization run: {command}. "
            f"Wait for the latest ci.yaml workflow_dispatch run at head {head} to finish successfully, "
            "then rerun the merge gate. This manual CI runs all validation lanes; never set release=true. "
            "The gate does not dispatch workflows. A different-head run cannot count.")


def verify_receipt(files, head, slug, runner, *, pr, now=None):
    """Fetch known-workflow receipts at the exact candidate head. No supplied URL/JSON."""
    classes = change_classes(files)
    record = {"classes": classes, "head": head, "regression_budget": 0, "regressions": 0}
    try:
        # A candidate cannot redefine the verifier which attests to itself.
        if any(f.get("path", "").startswith((".github/workflows/", ".github/actions/")) for f in files):
            raise ValueError("Verifier workflow changes require independent human review")
        if not head or not slug:
            raise ValueError("Missing repository or candidate head")
        clock = now or datetime.now(UTC)
        run, jobs = _trusted_run(head, slug, runner, "pull_request", clock)
        baseline = _baseline_evidence(run, head, slug, runner, pr)
        current = _bound_pr(pr, head, baseline, slug, runner)
        run_id, attempt = int(run["id"]), int(run["run_attempt"])
        passed = {j.get("name") for j in jobs if _passed(j, head, run_id)}
        for job in jobs:
            required_step = VALIDATION_STEPS.get(job.get("name"))
            if required_step and not _step_passed(job, required_step):
                passed.discard(job.get("name"))
        # Base-controlled regression data must actually run, not merely be collected.
        slices = {}
        for job in jobs:
            match = re.fullmatch(r"Python tests / Run tests slice (\d+)/(\d+)", job.get("name", ""))
            if match:
                index, total = map(int, match.groups())
                step = f"Run tests (slice {index}/{total})"
                if not _passed(job, head, run_id) or not any(s.get("name") == step and s.get("conclusion") == "success"
                                                          and s.get("status") == "completed" for s in job.get("steps", [])):
                    raise ValueError("Independent baseline test slice did not execute successfully")
                slices[index] = total
        if not slices or set(slices.values()) != {len(slices)} or set(slices) != set(range(1, len(slices) + 1)):
            raise ValueError("Missing complete independent baseline regression test slices")
        required = {gate for cls in classes for gate in CLASS_GATES[cls]}
        e2e = "Python tests / e2e"
        # PR CI intentionally omits E2E. Only this gate may use a supplemental
        # dispatch; baseline regression slices and every other gate stay PR-owned.
        if e2e in required:
            try:
                supplemental, supplemental_jobs = _trusted_run(head, slug, runner, "workflow_dispatch", clock)
                matching = [job for job in supplemental_jobs if job.get("name") == e2e]
                if not matching or not all(_passed(job, head, int(supplemental["id"]))
                                           and _step_passed(job, VALIDATION_STEPS[e2e]) for job in matching):
                    raise ValueError("Supplemental E2E validation step did not execute successfully")
            except (ValueError, TypeError, KeyError, OverflowError) as exc:
                raise ValueError(f"{exc}. {_dispatch_instruction(slug, current['head']['ref'], head)}") from exc
            passed.add(e2e)
            record.update(e2e_run_id=int(supplemental["id"]), e2e_run_attempt=int(supplemental["run_attempt"]),
                          e2e_validated_at=supplemental["updated_at"])
        missing = sorted(required - passed)
        if missing:
            raise ValueError("Missing successful class validation: " + ", ".join(missing))
        record.update(run_id=run_id, run_attempt=attempt, validated_at=run["updated_at"], baseline=baseline,
                      validation_scope="unchanged baseline regression suite", test_slices=len(slices), pr=pr)
        return record, []
    except (ValueError, TypeError, KeyError, OverflowError) as exc:
        return record, [f"ADMISSION_EVIDENCE: {exc}"]


def record_admission(record, *, repository, cycle, store=None):
    """Atomically account one exact head once per cycle; shared by concurrent merges."""
    if store is None:
        from hermes_constants import get_hermes_home
        store = get_hermes_home() / "evolution" / "admission.sqlite3"
    store = Path(store)
    store.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(store, timeout=30) as db:
        db.execute("CREATE TABLE IF NOT EXISTS admissions (repository TEXT, cycle TEXT, head TEXT, regressions INTEGER, receipt TEXT, PRIMARY KEY(repository, cycle, head))")
        db.execute("BEGIN IMMEDIATE")
        used = db.execute("SELECT COALESCE(SUM(regressions), 0) FROM admissions WHERE repository=? AND cycle=?", (repository, cycle)).fetchone()[0]
        prior = db.execute("SELECT regressions FROM admissions WHERE repository=? AND cycle=? AND head=?", (repository, cycle, record["head"])).fetchone()
        regressions = record["regressions"]
        if type(regressions) is not int or regressions < 0 or used + (0 if prior else regressions) > record["regression_budget"]:
            raise ValueError("ADMISSION_BUDGET: cycle regression budget exhausted")
        if prior is not None and prior[0] != regressions:
            raise ValueError("ADMISSION_BUDGET: same-head revalidation changed its regression count")
        db.execute("INSERT INTO admissions VALUES (?, ?, ?, ?, ?) "
                   "ON CONFLICT(repository, cycle, head) DO UPDATE SET receipt=excluded.receipt",
                   (repository, cycle, record["head"], regressions, json.dumps(record, sort_keys=True)))
        count = db.execute("SELECT COUNT(*) FROM admissions WHERE repository=? AND cycle=?", (repository, cycle)).fetchone()[0]
    return {"cycle": cycle, "admitted_heads": count, "regressions": used + (0 if prior else regressions), "regression_budget": record["regression_budget"]}
