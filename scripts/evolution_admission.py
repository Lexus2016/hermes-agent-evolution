"""Class-specific admission using GitHub-owned CI receipts, never self-grades (#3467)."""
from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

# The aggregate gate's check name carries a version (``(v2)`` today): a green run
# recorded on an old head must stop counting when the required set grows, so ci.yaml
# bumps the suffix and the protect-main ruleset bumps with it. Match the versioned
# name, never the literal one, or admission looks for a check no run produces.
GATE = "All required checks pass"
_GATE_NAME = re.compile(rf"^{re.escape(GATE)}( \(v\d+\))?$")


def _gate_reported(names):
    return any(_GATE_NAME.fullmatch(name) for name in names)


# All applicable policies apply to mixed changes. Parity is required; gains cannot
# buy regressions. GRASP's baseline-expanded budget is intentionally not used.
CLASS_GATES = {
    "memory-skill": (GATE, "Python lints / Agent Skills spec check (blocking)"),
    "tool": (GATE, "Python tests / e2e"),
    "control": (GATE, "Python tests / e2e", "Python lints / Windows footguns (blocking)"),
    "plugin": (GATE, "Python tests / e2e"),
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


def _baseline_evidence(run, head, slug, runner):
    prs = run.get("pull_requests", [])
    bases = {p.get("base", {}).get("sha") for p in prs if p.get("head", {}).get("sha") == head}
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
        machinery = (path.startswith((".github/", "scripts/ci/", "scripts/run_tests", "scripts/check"))
                     or Path(path).name in {"conftest.py", "pytest.ini", "setup.cfg", "sitecustomize.py", "usercustomize.py"})
        if baseline_test or machinery:
            raise ValueError(f"Independent baseline validation changed: {path}; requires human review")
    return base


def _passed(job, head, run_id):
    return (job.get("head_sha") == head and job.get("run_id") == run_id
            and job.get("status") == "completed" and job.get("conclusion") == "success")


def verify_receipt(files, head, slug, runner, *, now=None):
    """Fetch known-workflow receipts at the exact candidate head. No supplied URL/JSON."""
    classes = change_classes(files)
    record = {"classes": classes, "head": head, "regression_budget": 0, "regressions": 0}
    try:
        # A candidate cannot redefine the verifier which attests to itself.
        if any(f.get("path", "").startswith((".github/workflows/", ".github/actions/")) for f in files):
            raise ValueError("Verifier workflow changes require independent human review")
        if not head or not slug:
            raise ValueError("Missing repository or candidate head")
        pages = _api(runner, f"repos/{slug}/actions/workflows/ci.yaml/runs?head_sha={head}&event=pull_request&per_page=100")
        runs = [r for p in pages for r in p.get("workflow_runs", []) if r.get("head_sha") == head]
        if not runs:
            raise ValueError("Missing independent CI workflow receipt for candidate head")
        run = max(runs, key=lambda r: (r.get("id", 0), r.get("run_attempt", 0)))
        if (run.get("path") != ".github/workflows/ci.yaml" or run.get("event") != "pull_request"
                or run.get("repository", {}).get("full_name") != slug
                or run.get("status") != "completed" or run.get("conclusion") != "success"):
            raise ValueError("Latest independent CI run is not a completed successful trusted workflow")
        baseline = _baseline_evidence(run, head, slug, runner)
        completed = datetime.fromisoformat(run["updated_at"].replace("Z", "+00:00"))
        age = ((now or datetime.now(timezone.utc)) - completed).total_seconds()
        if not 0 <= age <= MAX_EVIDENCE_AGE_SECONDS:
            raise ValueError("CI evidence is stale or future-dated; rerun independent validation")
        run_id, attempt = int(run["id"]), int(run["run_attempt"])
        pages = _api(runner, f"repos/{slug}/actions/runs/{run_id}/attempts/{attempt}/jobs?per_page=100")
        jobs = [j for p in pages for j in p.get("jobs", [])]
        passed = {j.get("name") for j in jobs if _passed(j, head, run_id)}
        for job in jobs:
            required_step = VALIDATION_STEPS.get(job.get("name"))
            if required_step and not any(s.get("name") == required_step and s.get("status") == "completed"
                                         and s.get("conclusion") == "success" for s in job.get("steps", [])):
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
        missing = sorted(
            gate for cls in classes for gate in CLASS_GATES[cls]
            if not (_gate_reported(passed) if gate == GATE else gate in passed)
        )
        if missing:
            raise ValueError("Missing successful class validation: " + ", ".join(missing))
        record.update(run_id=run_id, run_attempt=attempt, validated_at=run["updated_at"], baseline=baseline,
                      validation_scope="unchanged baseline regression suite", test_slices=len(slices))
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
        prior = db.execute("SELECT 1 FROM admissions WHERE repository=? AND cycle=? AND head=?", (repository, cycle, record["head"])).fetchone()
        regressions = record["regressions"]
        if type(regressions) is not int or regressions < 0 or used + (0 if prior else regressions) > record["regression_budget"]:
            raise ValueError("ADMISSION_BUDGET: cycle regression budget exhausted")
        db.execute("INSERT OR IGNORE INTO admissions VALUES (?, ?, ?, ?, ?)", (repository, cycle, record["head"], regressions, json.dumps(record, sort_keys=True)))
        count = db.execute("SELECT COUNT(*) FROM admissions WHERE repository=? AND cycle=?", (repository, cycle)).fetchone()[0]
    return {"cycle": cycle, "admitted_heads": count, "regressions": used + (0 if prior else regressions), "regression_budget": record["regression_budget"]}
