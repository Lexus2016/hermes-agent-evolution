"""Tests for scripts/evolution_admission.py — the aggregate gate is a VERSIONED
check name (``All required checks pass (v2)`` today), so admission must match the
versioned context instead of looking for the bare literal, which no run produces.
"""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

import evolution_admission as ea  # noqa: E402

SLUG = "owner/repo"
HEAD = "a" * 40
BASE = "b" * 40
RUN_ID = 4242


def _job(name, steps=()):
    return {
        "name": name,
        "head_sha": HEAD,
        "run_id": RUN_ID,
        "status": "completed",
        "conclusion": "success",
        "steps": [{"name": s, "status": "completed", "conclusion": "success"} for s in steps],
    }


def _runner(gate_name, *, slice_job=True):
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    run = {
        "id": RUN_ID,
        "run_attempt": 1,
        "head_sha": HEAD,
        "path": ".github/workflows/ci.yaml",
        "event": "pull_request",
        "status": "completed",
        "conclusion": "success",
        "updated_at": now,
        "repository": {"full_name": SLUG},
        "pull_requests": [{"head": {"sha": HEAD}, "base": {"sha": BASE}}],
    }
    jobs = [_job("Python lints / Agent Skills spec check (blocking)", ("Run skills spec check",))]
    if gate_name:
        jobs.append(_job(gate_name))
    if slice_job:
        jobs.append(_job("Python tests / Run tests slice 1/1", ("Run tests (slice 1/1)",)))
    pages = {
        f"repos/{SLUG}/actions/workflows/ci.yaml/runs?head_sha={HEAD}&event=pull_request&per_page=100": [
            {"workflow_runs": [run]}
        ],
        f"repos/{SLUG}/compare/{BASE}...{HEAD}?per_page=100": [
            {"files": [{"filename": "docs/notes.md", "status": "modified"}]}
        ],
        f"repos/{SLUG}/actions/runs/{RUN_ID}/attempts/1/jobs?per_page=100": [{"jobs": jobs}],
    }

    def runner(cmd):
        endpoint = cmd[-1]
        if endpoint not in pages:
            return 1, "", "unexpected"
        return 0, json.dumps(pages[endpoint]), ""

    return runner


def test_versioned_gate_name_satisfies_admission():
    record, errors = ea.verify_receipt([{"path": "docs/notes.md"}], HEAD, SLUG, _runner("All required checks pass (v2)"))
    assert errors == []
    assert record["head"] == HEAD


def test_a_future_version_still_counts():
    record, errors = ea.verify_receipt([{"path": "docs/notes.md"}], HEAD, SLUG, _runner("All required checks pass (v3)"))
    assert errors == []


def test_missing_gate_still_fails_admission():
    record, errors = ea.verify_receipt([{"path": "docs/notes.md"}], HEAD, SLUG, _runner(None))
    assert errors and "All required checks pass" in errors[0]
