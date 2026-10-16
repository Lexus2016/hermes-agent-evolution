"""One-time debt requires exact provenance and separate reviewed compatibility evidence."""

import copy
import hashlib
import json
import subprocess
from dataclasses import asdict
from pathlib import Path

import pytest

from scripts.code_health import inherited_debt as policy
from scripts.code_health.inherited_evidence import assign_entries
from scripts.code_health.model import FileMeasure, Finding, Hit, Unit
from scripts.code_health.report import verdict


def _git(repo, *args):
    return subprocess.check_output(["git", *args], cwd=repo, text=True, timeout=60).strip()


def _commit(repo, files, message="fixture"):
    for path, text in files.items():
        target = repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", message)
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def history(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "Synthetic review")
    _git(repo, "config", "user.email", "synthetic@example.invalid")
    base = _commit(repo, {"base.txt": "fork\n"})
    upstream = _commit(repo, {"source.py": "def parent():\n    return 1\n"})
    raw = "[]\n"
    data = {"schema_version": 1, "base": base, "upstream": upstream,
            "report": {"path": "analysis/sync-health/raw.json", "sha256": hashlib.sha256(raw.encode()).hexdigest()},
            "entries": []}
    serialized = json.dumps(data)
    head = _commit(repo, {policy.MANIFEST: serialized, data["report"]["path"]: raw})
    monkeypatch.setattr(policy, "BASE", base)
    monkeypatch.setattr(policy, "UPSTREAM", upstream)
    monkeypatch.setattr(policy, "MANIFEST_SHA256", hashlib.sha256(serialized.encode()).hexdigest())
    return repo, base, upstream, head


def test_transition_requires_actual_parent_ancestry(history):
    repo, base, _, head = history
    assert policy._policy(repo, base, head)["entries"] == []
    _git(repo, "checkout", "--orphan", "unrelated")
    detached = _commit(repo, {"detached.txt": "no parent relationship"})
    with pytest.raises(RuntimeError, match="true pinned"):
        policy._policy(repo, base, detached)


def test_unrelated_repo_without_manifest_uses_ordinary_gate(tmp_path):
    _git(tmp_path, "init", "-q")
    assert policy._policy(tmp_path, "absent-base", None) is None


def test_changed_base_cannot_reuse_transition(history):
    repo, _, _, head = history
    root = _git(repo, "rev-list", "--max-parents=0", "HEAD")
    _git(repo, "checkout", "--detach", root)
    different = _commit(repo, {"different.txt": "new fork base"})
    with pytest.raises(RuntimeError, match="baseline differs"):
        policy._policy(repo, different, head)


def test_postmerge_base_uses_ordinary_ratchet_even_if_manifest_changed(history):
    repo, _, _, merged = history
    new_head = _commit(repo, {policy.MANIFEST: "changed policy cannot buy a future exemption"})
    assert policy._policy(repo, merged, new_head) is None
    assert verdict([Finding("p.py", "BLE001", "f", 1, "new bug")]) == (1, 0)


def test_missing_pinned_history_fails_transition(history, monkeypatch):
    repo, base, _, head = history
    monkeypatch.setattr(policy, "UPSTREAM", "f" * 40)
    with pytest.raises(RuntimeError, match="object missing"):
        policy._policy(repo, base, head)


def test_digest_detects_drift_not_human_authenticity(history):
    repo, base, _, _ = history
    head = _commit(repo, {policy.MANIFEST: "{}"})
    with pytest.raises(RuntimeError, match="manifest drift"):
        policy._policy(repo, base, head)


def test_original_full_report_must_survive(history):
    repo, base, _, _ = history
    head = _commit(repo, {"analysis/sync-health/raw.json": "[] changed"})
    with pytest.raises(RuntimeError, match="full code-health report"):
        policy._policy(repo, base, head)


@pytest.mark.platforms("posix")
def test_symlink_evidence_is_not_read(tmp_path):
    (tmp_path / "link").symlink_to(tmp_path / "target")
    with pytest.raises(RuntimeError, match="regular repository"):
        policy.blob(tmp_path, None, "link")


def _evidence():
    fm = FileMeasure("p.py", metrics={"FILE_LINES": 4},
                     units={"f": Unit("f", 1, {"CC": 21}, "same-body")}, lines=["def f():", "    except Exception:"])
    fm.add_hit("BLE001", "f", 2)
    old = copy.deepcopy(fm)
    entry = {"id": "reviewed-occurrence", "status": "reviewed", "path": "p.py", "candidate_blob": "blob",
             "source": {"commit": "parent", "path": "p.py", "blob": "blob", "scope": "f", "unit_body_hash": "same-body"},
             "rule": "BLE001", "scope": "f", "code": "except Exception:", "detail": "blind except", "max_occurrences": 1,
             "review": {"reviewer": "synthetic-test-only", "receipt": "Synthetic independent review fixture, not a production receipt",
                        "compatibility": "Synthetic error boundary validated by the listed regression fixture",
                        "module_bindings": "Synthetic fixture has unchanged imports and global dependencies"},
             "validation": [{"path": "tests/evidence.py", "blob": "test-blob", "outcome": "passed",
                             "claim": "Synthetic regression preserves the intended failure boundary"}]}
    finding = Finding("p.py", "BLE001", "f", 2, "blind except")
    blobs = {("head", "p.py"): ("blob", b"code"), ("parent", "p.py"): ("blob", b"code"),
             ("head", "tests/evidence.py"): ("test-blob", b"test")}
    return fm, old, entry, finding, blobs


def _assign(fm, old, entries, findings, blobs):
    assign_entries(entries, findings, {"p.py": fm}, {"parent": {"p.py": old}},
                   lambda rev, path: blobs.get((rev, path)), "head", "reviewed-digest")


def test_reviewed_debt_retains_original_raw_severity_and_detail():
    fm, old, entry, finding, blobs = _evidence()
    _assign(fm, old, [entry], [finding], blobs)
    assert finding.blocking is True and finding.allowed_reason is None
    assert finding.detail == "blind except" and asdict(finding)["inherited_debt"]["entry"] == entry["id"]
    assert verdict([finding]) == (0, 0)


@pytest.mark.parametrize("status", ["pending", "unknown", "proposed"])
def test_unreviewed_cases_remain_blocking(status):
    fm, old, entry, finding, blobs = _evidence()
    entry["status"] = status
    _assign(fm, old, [entry], [finding], blobs)
    assert verdict([finding]) == (1, 0)


@pytest.mark.parametrize("change", ["count", "source-hit", "new-file", "evidence", "placeholder", "duplicate"])
def test_false_or_stale_evidence_fails_closed(change):
    fm, old, entry, finding, blobs = _evidence()
    entries = [entry]
    mutations = {
        "count": lambda: entry.update(max_occurrences=2),
        "source-hit": old.hits.clear,
        "new-file": lambda: blobs.update({("head", "p.py"): ("different", b"changed code")}),
        "evidence": lambda: entry["validation"][0].update(outcome="pending"),
        "placeholder": lambda: entry["review"].update(compatibility="TODO: review this later before acceptance"),
        "duplicate": lambda: entries.append(copy.deepcopy(entry)),
    }
    mutations[change]()
    with pytest.raises(RuntimeError):
        _assign(fm, old, entries, [finding], blobs)


def test_additional_occurrence_and_new_rule_are_not_paid_for():
    fm, old, entry, finding, blobs = _evidence()
    with pytest.raises(RuntimeError, match="New inherited-debt occurrence"):
        _assign(fm, old, [entry], [finding, copy.deepcopy(finding)], blobs)
    fresh = Finding("p.py", "S110", "f", 2, "new swallowed exception")
    _assign(fm, old, [entry], [finding, fresh], blobs)
    assert verdict([finding, fresh]) == (1, 0)


def test_metric_growth_cannot_be_inherited():
    fm, old, entry, finding, blobs = _evidence()
    entry.update(rule="CC", detail="CC 22", max_occurrences=1)
    finding.rule, finding.detail = "CC", "CC 22"
    fm.units["f"].metrics["CC"] = 22
    with pytest.raises(RuntimeError, match="New metric growth"):
        _assign(fm, old, [entry], [finding], blobs)


def test_changed_module_requires_matching_unit_and_binding_review():
    fm, old, entry, finding, blobs = _evidence()
    entry["candidate_blob"] = "modified"
    blobs[("head", "p.py")] = ("modified", b"changed module")
    entry["review"].pop("module_bindings")
    with pytest.raises(RuntimeError, match="global bindings"):
        _assign(fm, old, [entry], [finding], blobs)
    entry["review"]["module_bindings"] = "Reviewed exact imported/global values with regression evidence"
    fm.units["f"].body_hash = "different-body"
    with pytest.raises(RuntimeError, match="exact reviewed parent unit"):
        _assign(fm, old, [entry], [finding], blobs)


def test_health_runtime_budget_does_not_extend_other_checks(monkeypatch):
    import runpy
    runner = runpy.run_path(str(Path(__file__).resolve().parents[2] / "scripts/check"))
    jobs = runner["_jobs"](["health", "shebangs"], None, None)
    assert jobs["health"].timeout == 3600
    assert jobs["shebangs"].timeout == 900
    observed = []

    def finished(*args, **kwargs):
        observed.append(kwargs["timeout"])
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", finished)
    runner["_run"](jobs["health"])
    runner["_run"](jobs["shebangs"])
    assert observed == [3600, 900]


def test_health_timeout_remains_failure(monkeypatch):
    import runpy
    runner = runpy.run_path(str(Path(__file__).resolve().parents[2] / "scripts/check"))
    def timed_out(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], kwargs["timeout"])

    monkeypatch.setattr(subprocess, "run", timed_out)
    with pytest.raises(subprocess.TimeoutExpired):
        runner["_run"](runner["_Job"](["unused"], timeout=3600))


def test_actual_measurer_preserves_raw_json_and_blocks_unreviewed_rule(tmp_path, monkeypatch, capsys):
    from scripts.code_health.cli import run
    from scripts.code_health.compare import compare
    from scripts.code_health.measure import Measurer
    from scripts.code_health.ruff_runner import resolve_ruff

    repo = tmp_path / "measured"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "Synthetic review")
    _git(repo, "config", "user.email", "synthetic@example.invalid")
    checkout = Path(__file__).resolve().parents[2]
    base = _commit(repo, {
        "base.txt": "original fork",
        "pyproject.toml": (checkout / "pyproject.toml").read_text(encoding="utf-8-sig"),
        "scripts/ci/profile_scope_patterns.json": (checkout / "scripts/ci/profile_scope_patterns.json").read_text(encoding="utf-8-sig"),
    })
    code = "def f():\n    try:\n        return 1\n    except Exception:\n        pass\n"
    upstream = _commit(repo, {"p.py": code, "tests/evidence.py": "def test_boundary():\n    assert True\n"})
    measurer = Measurer(repo, resolve_ruff(checkout), known_env=set())
    measured = measurer.measure(upstream, ["p.py"])
    findings = compare({}, measured, [])
    caught = next(f for f in findings if f.rule == "BLE001")
    _, _, entry, _, _ = _evidence()
    entry.update(candidate_blob=policy.blob(repo, upstream, "p.py")[0], detail=caught.detail,
                 code=measured["p.py"].code_line(caught.line))
    entry["source"].update(commit=upstream, blob=entry["candidate_blob"])
    entry["validation"][0]["blob"] = policy.blob(repo, upstream, "tests/evidence.py")[0]
    raw = json.dumps([asdict(f) for f in findings])
    manifest = {"schema_version": 1, "base": base, "upstream": upstream, "entries": [entry],
                "report": {"path": "analysis/sync-health/raw.json", "sha256": hashlib.sha256(raw.encode()).hexdigest()}}
    serialized = json.dumps(manifest)
    head = _commit(repo, {policy.MANIFEST: serialized, manifest["report"]["path"]: raw})
    monkeypatch.setattr(policy, "BASE", base)
    monkeypatch.setattr(policy, "UPSTREAM", upstream)
    monkeypatch.setattr(policy, "MANIFEST_SHA256", hashlib.sha256(serialized.encode()).hexdigest())
    assert run(repo, base, head, as_json=True) == 1
    result = json.loads(capsys.readouterr().out)
    broad = next(f for f in result if f["rule"] == "BLE001")
    silent = next(f for f in result if f["rule"] == "S110")
    assert broad["blocking"] is True and broad["inherited_debt"] is not None
    assert silent["blocking"] is True and silent["inherited_debt"] is None


@pytest.mark.parametrize("copied", ["file", "unit"])
def test_retained_original_cannot_fund_new_copy_without_own_finding(copied):
    fm, old, entry, finding, blobs = _evidence()
    # Only the newly copied code is reported: the retained original has no new
    # finding. It still owns its source occurrence and must not fund the copy.
    if copied == "file":
        entry["path"] = finding.path = "copy.py"
        fm.path = "copy.py"
        blobs[("head", "copy.py")] = ("blob", b"code")
    else:
        entry["scope"] = finding.scope = "copied_function"
        fm.units["copied_function"] = copy.deepcopy(fm.units["f"])
    with pytest.raises(RuntimeError, match="cannot relocate or rename"):
        assign_entries([entry], [finding], {fm.path: fm}, {"parent": {"p.py": old}},
                       lambda rev, path: blobs.get((rev, path)), "head", "reviewed-digest")


def test_reviewed_baseline_advance_rejects_previous_base(history, monkeypatch):
    """A reviewed fork advance needs a new exact pin, not a baseline wildcard."""
    repo, previous, upstream, integration = history
    _git(repo, "checkout", "--detach", previous)
    advanced = _commit(repo, {"security-fix.txt": "reviewed fork correction\n"})
    _git(repo, "merge", "--no-ff", integration, "-m", "retain both histories")
    data = json.loads((repo / policy.MANIFEST).read_text(encoding="utf-8-sig"))
    original_report = copy.deepcopy(data["report"])
    data["base"] = advanced
    serialized = json.dumps(data)
    candidate = _commit(repo, {policy.MANIFEST: serialized})
    monkeypatch.setattr(policy, "BASE", advanced)
    monkeypatch.setattr(policy, "MANIFEST_SHA256", hashlib.sha256(serialized.encode()).hexdigest())

    reviewed = policy._policy(repo, advanced, candidate)
    assert reviewed["report"] == original_report
    assert reviewed["upstream"] == upstream
    with pytest.raises(RuntimeError, match="baseline differs"):
        policy._policy(repo, previous, candidate)
    # Once this integration becomes the base, ordinary future ratchets resume.
    later = _commit(repo, {policy.MANIFEST: "future edits do not extend the exception"})
    assert policy._policy(repo, candidate, later) is None
