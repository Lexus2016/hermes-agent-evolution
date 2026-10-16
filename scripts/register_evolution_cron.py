#!/usr/bin/env python3
"""Register Hermes Evolution cron jobs into the native Hermes cron registry.

Why this exists
---------------
Evolution ships its scheduled tasks as rich custom YAML under
``cron/evolution/*.yaml`` (name, schedule, prompt, skills, toolsets, github,
output, limits). But Hermes schedules jobs ONLY from its native registry
``~/.hermes/cron/jobs.json`` (see ``cron/jobs.py``). Copying the YAML files
into a folder does nothing — the scheduler never reads them.

This converter maps each evolution YAML onto a native job via the canonical
``cron.jobs.create_job`` API, so the jobs actually run. It is **idempotent by
job name**: re-running it (e.g. on every upgrade) never creates duplicates.

Skill id normalization
----------------------
Evolution YAML references skills as ``evolution/research``, but the bundled
skill's canonical name is ``evolution-research``. We normalize ``/`` -> ``-``
so the scheduler resolves the real skill (``evolution/analysis`` ->
``evolution-analysis``, etc.).

Usage
-----
    python scripts/register_evolution_cron.py [--dry-run] [SRC_DIR]

Exit codes: 0 ok, 1 setup error, 2 one or more jobs failed to register.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

# Standalone source invocations must resolve the canonical repository YAML policy.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _find_venv_python(repo_root: Path) -> str | None:
    """Locate the install's venv interpreter (has the full Hermes deps)."""
    for rel in (
        "venv/bin/python",
        ".venv/bin/python",
        "venv/bin/python3",
        ".venv/bin/python3",
    ):
        cand = repo_root / rel
        if cand.is_file() and os.access(cand, os.X_OK):
            return str(cand)
    return None


def _ensure_venv_python(repo_root: Path, argv: list[str]) -> None:
    """Guarantee we run under the install's venv interpreter.

    The system python typically lacks the FULL Hermes dependency set (dotenv,
    croniter, …), so importing cron.jobs and parsing schedules would fail in
    assorted ways depending on which dep is missing first. Rather than play
    whack-a-mole, re-exec under the venv python up front so the registrar
    "just works" regardless of which interpreter launched it — no human OR
    agent ever has to pick the interpreter. No-op when already on the venv
    python (``samefile`` follows symlinks) or when no venv is found. Loop-guarded
    via ``_HERMES_REG_REEXEC`` so a single re-exec can never recurse.
    """
    if os.environ.get("_HERMES_REG_REEXEC") == "1":
        return
    venv_py = _find_venv_python(repo_root)
    if not venv_py:
        return
    try:
        if os.path.samefile(sys.executable, venv_py):
            return  # already the venv interpreter
    except OSError:
        pass
    os.environ["_HERMES_REG_REEXEC"] = "1"
    print(
        f"[evolution-cron] re-executing under venv python: {venv_py}",
        file=sys.stderr,
    )
    os.execv(venv_py, [venv_py, str(Path(__file__).resolve()), *argv[1:]])


def _load_yaml(path: Path) -> dict:
    import hermes_yaml as yaml   # Use the canonical Hermes YAML policy.

    with open(path, "r", encoding="utf-8-sig") as fh:
        return yaml.safe_load(fh) or {}


def _normalize_skills(raw) -> list[str] | None:
    """evolution/research -> evolution-research; drop blanks."""
    if not raw:
        return None
    if isinstance(raw, str):
        raw = [raw]
    out = [str(s).strip().replace("/", "-") for s in raw if str(s).strip()]
    return out or None


def _normalize_toolsets(raw) -> list[str] | None:
    """Expand short toolset names to canonical forms and append `delegation` for bulky jobs."""
    if not raw:
        return None
    if isinstance(raw, str):
        raw = [raw]
    out = [str(s).strip() for s in raw if str(s).strip()]
    # Bulk-heavy evolution stages (research/introspection) benefit from subagent
    # delegation so large reads do not inflate the main job context.
    if "delegation" not in out:
        out.append("delegation")
    return out or None


def _load_skill_lint(repo_root: Path):
    """Import the sibling ``evolution_skill_lint`` module (the CI lint's pure
    core) so registration can run the same skill→toolset wiring check before a
    broken job is ever scheduled (#702). Returns None when the module is
    missing or unimportable — registration then proceeds without pre-flight."""
    import importlib.util

    path = repo_root / "scripts" / "evolution_skill_lint.py"
    if not path.is_file():
        return None
    try:
        spec = importlib.util.spec_from_file_location("evolution_skill_lint", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    except Exception as exc:  # pragma: no cover - environment dependent
        print(
            f"[evolution-cron] warning: cannot load evolution_skill_lint: {exc}",
            file=sys.stderr,
        )
        return None


def _validate_skill_toolsets(
    name: str,
    raw_skills,
    toolsets: list[str] | None,
    lint_mod,
    skill_texts: dict,
    existing_scripts: set,
) -> str | None:
    """Pre-flight the job's skill→toolset wiring at registration time (#702).

    ``evolution_skill_lint`` already detects this class of bug in CI, but CI
    runs AFTER the broken job has been scheduled. Blocking here prevents e.g.
    a job whose skills instruct running ``scripts/X.py`` from registering
    without the ``terminal`` toolset — that job could only ever no-op.

    Only the ``no_terminal`` violation class blocks: ``missing_skill`` /
    ``missing_script`` may be false positives for skills installed outside the
    repo, so they are surfaced as warnings instead. Returns the blocking
    reason, or None when the job is clean.
    """
    if lint_mod is None or not raw_skills:
        return None
    if isinstance(raw_skills, str):
        raw_skills = [raw_skills]
    stage = {
        "name": name,
        "skills": [str(s) for s in raw_skills],
        "toolsets": list(toolsets or []),
    }
    violations = lint_mod.find_violations([stage], skill_texts, existing_scripts)
    blocking = [v for v in violations if v["kind"] == "no_terminal"]
    for v in violations:
        if v["kind"] != "no_terminal":
            print(
                f"[evolution-cron] warning: {name}/{v['skill']}: {v['detail']}",
                file=sys.stderr,
            )
    if blocking:
        return "; ".join(f"skill '{v['skill']}': {v['detail']}" for v in blocking)
    return None


def _install_script(repo_root: Path, filename: str) -> str | None:
    """Copy a repo script into HERMES_HOME/scripts (the only place the cron
    scheduler is allowed to execute scripts from). Returns the script name on
    success, None on failure.
    """
    import os
    import shutil

    src = repo_root / "scripts" / filename
    if not src.is_file():
        return None
    home = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
    dest_dir = home / "scripts"
    try:
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / src.name
        shutil.copyfile(src, dest)
        dest.chmod(0o755)
        return src.name
    except Exception as exc:  # pragma: no cover - environment dependent
        print(
            f"[evolution-cron] warning: could not install script {filename}: {exc}",
            file=sys.stderr,
        )
        return None


def _install_access_gate(repo_root: Path) -> str | None:
    """Copy the GitHub access wake-gate into HERMES_HOME/scripts so cron runs it
    as a pre-check before each evolution job. Returns the script name to attach
    to every job, or None on failure (jobs are still created, just ungated).

    The gate prints ``{"wakeAgent": false}`` when GitHub is unreachable, which
    makes the scheduler SKIP the LLM agent entirely — no tokens / web-search
    spent when the cycle has no outlet to post issues/PRs.
    """
    return _install_script(repo_root, "evolution_access_gate.sh")


def _install_local_gate(repo_root: Path) -> str | None:
    """Class-B local-signal wake-gate (introspection). No GitHub call."""
    return _install_script(repo_root, "evolution_local_gate.sh")


def _install_issues_gate(repo_root: Path) -> str | None:
    """Class-A issues gate: write access AND a fresh research batch."""
    return _install_script(repo_root, "evolution_issues_batch_gate.sh")


# Jobs that produce GitHub artifacts (issues/PRs/merges) or need repo write.
# Registrar pauses these when write is confirmed absent; wake-gates skip LLM.
CLASS_A_JOBS = frozenset({
    "evolution-research",
    "evolution-issues",
    "evolution-analysis",
    "evolution-implementation",
    "evolution-integration",
    "evolution-hydra",
    "evolution-upstream-sync",
    "evolution-ci-diagnosis",
    "evolution-pr-reflection",
    "evolution-postmortem-miner",
    "evolution-meta-analysis",
})

# Local jobs: no GitHub write required. Watchdog is no_agent and always ticks.
CLASS_B_JOBS = frozenset({
    "evolution-watchdog",
    "evolution-introspection",
    "evolution-memory-poison-scan",
    "evolution-config-mutation-scan",
    "evolution-dream-contradiction-scan",
    "evolution-utility-audit",
    "evolution-experience-harvest",
    "evolution-proceed-hold-calibration",
    "evolution-harness-gate",
    "evolution-rubric-judge",
    "evolution-funnel",
})

PAUSE_REASON_NO_WRITE = "evolution: no GitHub write access"

_DEFAULT_EVOLUTION_CONFIG = {
    "cadence": "chat-safe",
    "research_interval_days": 7,
}


def _is_class_a(name: str, spec: dict | None = None) -> bool:
    if name in CLASS_B_JOBS:
        return False
    if name in CLASS_A_JOBS:
        return True
    spec = spec or {}
    # A job that declares a GitHub outlet is class A even if newly named.
    return bool(spec.get("github"))


def _load_evolution_config() -> dict:
    """Read ``evolution:`` from config.yaml; chat-safe defaults otherwise."""
    out = dict(_DEFAULT_EVOLUTION_CONFIG)
    try:
        from hermes_constants import get_hermes_home

        path = get_hermes_home() / "config.yaml"
        if not path.is_file():
            return out
        raw = _load_yaml(path)
        section = raw.get("evolution") if isinstance(raw, dict) else None
        if not isinstance(section, dict):
            return out
        cadence = str(section.get("cadence") or out["cadence"]).strip().lower()
        if cadence in ("chat-safe", "dense"):
            out["cadence"] = cadence
        try:
            days = int(section.get("research_interval_days", out["research_interval_days"]))
        except (TypeError, ValueError):
            days = out["research_interval_days"]
        out["research_interval_days"] = max(5, min(10, days))
    except Exception:
        return out
    return out


def _pick_schedule(spec: dict, cadence: str, name: str, research_interval_days: int) -> str:
    """YAML ``schedule`` is chat-safe; ``schedule_dense`` is the owner-bot overlay."""
    chat = str(spec.get("schedule") or "").strip()
    dense = str(spec.get("schedule_dense") or "").strip()
    chosen = dense if cadence == "dense" and dense else chat
    if name == "evolution-research" and cadence != "dense":
        days = int(research_interval_days or 7)
        if days != 7:
            return f"every {days}d"
    return chosen


def _classify_write_access() -> str:
    """``write`` / ``denied`` / ``inconclusive`` — never raises."""
    try:
        script_dir = Path(__file__).resolve().parent
        sys.path.insert(0, str(script_dir))
        from evolution_github_access import classify as _classify

        return _classify()
    except Exception:
        return "inconclusive"


def _resume_paused_no_write(job: dict, update_job, resume_job) -> bool:
    """Resume a class-A job we previously paused for missing write access."""
    if (job.get("paused_reason") or "") != PAUSE_REASON_NO_WRITE:
        return False
    if job.get("enabled", True) and job.get("state") != "paused":
        return False
    try:
        resume_job(job["id"])
        return True
    except Exception:
        return False



def _evolution_sibling_modules(repo_root: Path, family: list[Path]) -> list[str]:
    """Bare-name script modules the ``evolution_*`` family imports from its own dir.

    ``_install_evolution_helpers`` mirrors the family by an ``evolution_*.py``
    glob, but the family's imports reach OUTSIDE that prefix: ``evolution_funnel``
    imports ``delegation_escalation_report`` and ``evolution_evaluator`` imports
    ``eval_tasks``. Neither was ever installed, so the copy the scheduler runs
    (HERMES_HOME/scripts — the only directory it may execute from) raised
    ImportError on the import site and the whole job died: evolution-funnel
    failed on 2026-10-16 the first time its daily slot ran after the funnel copy
    was refreshed. Only the hand-added ``introspection_extract.py`` install below
    kept ``evolution_trace_miner`` alive, which is exactly the maintenance this
    derivation removes.

    Derived from the family's own imports rather than a hand-kept list, because a
    list rots the moment a new sibling appears (``eval_tasks`` was already
    waiting to bite). Only modules that really exist as ``scripts/<name>.py`` are
    returned, so unrelated files in that directory are never dragged in.
    """
    import ast

    candidates: set[str] = set()
    for path in family:
        try:
            tree = ast.parse(path.read_text(encoding="utf-8-sig"))
        except (OSError, SyntaxError, ValueError):
            continue  # fail-open: an unparseable file must not block the deploy
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                candidates.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and not node.level and node.module:
                # Both `from delegation_escalation_report import x` (the form that
                # resolves in a copy) and `from scripts.<name> import x` (the form
                # that resolves in a checkout) name the same sibling file.
                candidates.add(node.module.split(".")[-1])

    return [
        f"{name}.py"
        for name in sorted(candidates)
        if not name.startswith("evolution_")
        and (repo_root / "scripts" / f"{name}.py").is_file()
    ]


def _install_evolution_helpers(repo_root: Path) -> list[str]:
    """Install the whole ``evolution_*.py`` script family into HERMES_HOME/scripts.

    The per-job loop installs only each no_agent job's own ``script``. But those
    scripts IMPORT siblings — e.g. evolution_funnel imports evolution_metrics and
    evolution_realized_impact to refresh its health/realized-impact sidecars. A
    sibling that lives only in the repo checkout (not in HERMES_HOME/scripts, where
    the scheduler runs) raises ImportError at runtime and the refresh silently
    no-ops (the import is guarded), so the sidecars freeze and the watchdog reads
    stale health. Installing the whole family keeps every intra-family import
    resolvable from the one directory the scheduler executes from. Future helper
    scripts are picked up automatically by the glob.

    The family's imports do not stop at the prefix, so the non-``evolution_*``
    siblings they import are installed too — see ``_evolution_sibling_modules``
    for why that derivation is not a hand-kept list.
    """
    installed: list[str] = []
    family = sorted((repo_root / "scripts").glob("evolution_*.py"))
    for src in family:
        if _install_script(repo_root, src.name):
            installed.append(src.name)
    for name in _evolution_sibling_modules(repo_root, family):
        if _install_script(repo_root, name):
            installed.append(name)
    return installed


# Labels required by the evolution pipeline.  Kept in one place so every skill
# stage (issues, introspection, integration, implementation) can rely on them
# existing.  Creation is idempotent; failures are warnings, not fatal.
_EVOLUTION_LABELS: list[tuple[str, str, str]] = [
    ("capability", "5319e7", "Missing ability users needed"),
    ("introspection", "0e8a16", "Found by session introspection"),
    ("ux", "fbca04", "Interaction friction"),
    ("proposal", "0e8a16", "Evolution-generated improvement proposal"),
    ("research-generated", "1d76db", "Created by the evolution research cycle"),
    ("needs-work", "d93f0b", "Blocked by code-review (dead code / not integrated)"),
    ("next-increment", "1d76db", "Roadmap increment merged; more deferred — re-queued"),
    ("accepted", "0e8a16", "Accepted by evolution — sent to a PR / implemented"),
    ("rejected", "b60205", "Not accepted by evolution — see closing comment"),
    ("needs-split", "d4c5f9", "Wanted, but exceeds one cycle — needs decomposition"),
    ("blocked", "e11d21", "Needs human/infrastructure action — see comment"),
    ("fix", "1d76db", "Bug or fix"),
    ("improvement", "a2eeef", "An improvement to existing functionality"),
    (
        "implemented-on-main",
        "0e8a16",
        "Capability already exists on main — no code change needed",
    ),
]


def _ensure_evolution_labels(repo_root: Path, dry_run: bool = False) -> list[str]:
    """Idempotently create the GitHub labels used by the evolution pipeline.

    Several evolution skills call ``gh label create`` with the expectation that
    the label exists; on a fresh fork the labels are missing and every label
    operation fails silently (wasting API calls and leaving issues
    uncategorized — #468). This bootstrap step runs once per registration pass.

    Returns the list of label names that were created or confirmed present.
    Warnings are printed for any failure, but registration continues.
    """
    import subprocess

    if not dry_run and shutil.which("gh") is None:
        print(
            "[evolution-cron] warning: gh CLI is not installed; GitHub labels "
            "were not created and GitHub-dependent Evolution stages cannot run. "
            "/ gh CLI не встановлено; мітки GitHub не створено, а залежні від "
            "GitHub етапи Evolution не зможуть працювати.",
            file=sys.stderr,
        )
        return []

    created: list[str] = []
    for name, color, description in _EVOLUTION_LABELS:
        cmd = [
            "gh",
            "label",
            "create",
            name,
            "--repo",
            "Lexus2016/hermes-agent-evolution",
            "--color",
            color,
            "--description",
            description,
        ]
        if dry_run:
            print(f"[evolution-cron] dry-run label: {name}")
            created.append(name)
            continue
        try:
            result = subprocess.run(
                cmd,
                cwd=repo_root,
                capture_output=True,
                text=True,
                check=False,
                timeout=30,
            )
            if result.returncode == 0 or "already exists" in (result.stderr or "").lower():
                created.append(name)
            else:
                print(
                    f"[evolution-cron] warning: could not create label {name}: "
                    f"{result.stderr or result.stdout}",
                    file=sys.stderr,
                )
        except Exception as exc:  # pragma: no cover - gh may be missing
            print(
                f"[evolution-cron] warning: could not create label {name}: {exc}",
                file=sys.stderr,
            )
    return created


def main(argv: list[str]) -> int:
    dry_run = "--dry-run" in argv
    positional = [a for a in argv[1:] if not a.startswith("--")]

    repo_root = Path(__file__).resolve().parent.parent

    # Before importing ANY Hermes module, make sure we're on the venv python
    # that actually has the dependencies (dotenv, croniter, …). This replaces
    # the process when needed, so nobody has to launch us with the right python.
    _ensure_venv_python(repo_root, argv)

    # Bootstrap the GitHub labels used by every evolution skill.  Missing labels
    # make issue/PR operations fail silently on fresh forks (#468).
    label_ensured = [] if dry_run else _ensure_evolution_labels(repo_root)

    src_dir = Path(positional[0]) if positional else repo_root / "cron" / "evolution"
    if not src_dir.is_dir():
        print(f"[evolution-cron] no evolution cron dir at {src_dir}", file=sys.stderr)
        return 1

    # Import the canonical Hermes cron API (writes ~/.hermes/cron/jobs.json).
    sys.path.insert(0, str(repo_root))
    try:
        from cron.jobs import (
            create_job,
            load_jobs,
            parse_schedule,
            pause_job,
            resume_job,
            update_job,
        )
    except Exception as exc:  # pragma: no cover - environment dependent
        print(f"[evolution-cron] cannot import cron.jobs: {exc}", file=sys.stderr)
        return 1

    evo_cfg = _load_evolution_config()
    cadence = evo_cfg["cadence"]
    research_days = evo_cfg["research_interval_days"]
    write_state = "write" if dry_run else _classify_write_access()
    print(
        f"[evolution-cron] cadence={cadence} research_interval_days={research_days} "
        f"github_write={write_state}"
    )

    # Install the GitHub-access wake-gate and attach it to class-A jobs
    # so the expensive LLM agent only runs when GitHub write is confirmed.
    gate_script = None if dry_run else _install_access_gate(repo_root)
    local_gate = None if dry_run else _install_local_gate(repo_root)
    issues_gate = None if dry_run else _install_issues_gate(repo_root)
    if not dry_run:
        _install_script(repo_root, "evolution_github_access.py")
        _install_script(repo_root, "introspection_extract.py")

    # Install the whole evolution_* helper family so no_agent scripts' sibling
    # imports (funnel -> metrics/realized_impact) resolve in HERMES_HOME/scripts.
    helper_scripts = [] if dry_run else _install_evolution_helpers(repo_root)

    # Pre-flight context for the skill→toolset wiring check (#702): loaded once,
    # reused for every agent job below.
    lint_mod = _load_skill_lint(repo_root)
    if lint_mod is not None:
        skill_texts = lint_mod._load_skill_texts(repo_root / "skills")
        existing_scripts = {
            f"scripts/{p.name}"
            for p in (repo_root / "scripts").glob("*")
            if p.is_file()
        }
    else:
        skill_texts, existing_scripts = {}, set()
        print(
            "[evolution-cron] warning: evolution_skill_lint.py not found — "
            "skipping skill→toolset pre-flight",
            file=sys.stderr,
        )

    existing_jobs = {str(j.get("name", "")).strip(): j for j in load_jobs()}
    existing_names = set(existing_jobs)
    created: list[tuple[str, str]] = []
    updated: list[tuple[str, str]] = []
    skipped: list[str] = []
    failed: list[tuple[str, str]] = []

    for yaml_file in sorted(src_dir.glob("*.yaml")):
        try:
            spec = _load_yaml(yaml_file)
        except Exception as exc:
            failed.append((yaml_file.name, f"yaml parse error: {exc}"))
            continue

        name = str(spec.get("name") or yaml_file.stem).strip()
        class_a = _is_class_a(name, spec)

        # Three-state GitHub write handling (council 2026-08-31):
        # write        → register/reconcile class A
        # denied       → do not create class A; pause (never delete) existing
        # inconclusive → leave existing class A untouched; do not create new
        existing = existing_jobs.get(name)
        if class_a and write_state == "denied":
            if existing and existing.get("id"):
                if not dry_run:
                    pause_job(existing["id"], reason=PAUSE_REASON_NO_WRITE)
                skipped.append(name)
                print(
                    f"[evolution-cron] paused class-A job '{name}' "
                    f"(no GitHub write access)"
                )
                continue
            if existing is None:
                skipped.append(name)
                print(
                    f"[evolution-cron] skip new class-A job '{name}' "
                    f"(no GitHub write access)"
                )
                continue
        if class_a and write_state == "inconclusive" and existing is None:
            skipped.append(name)
            print(
                f"[evolution-cron] skip new class-A job '{name}' "
                f"(GitHub write check inconclusive)"
            )
            continue
        if class_a and write_state == "write" and existing and existing.get("id"):
            if _resume_paused_no_write(existing, update_job, resume_job):
                print(f"[evolution-cron] resumed class-A job '{name}' (write restored)")

        # Refresh any YAML-declared script on EVERY run — no_agent AND
        # per-job agent-gate scripts (Hydra's evolution_hydra_gate.py,
        # evolution-analysis's evolution_analysis_gate.sh, etc.) alike —
        # including for already-registered jobs, mirroring the access gate
        # above: `hermes update` refreshes the repo checkout, but the
        # scheduler executes the copy in HERMES_HOME/scripts. Without this
        # refresh, two things go stale: (a) the installed script's CONTENT
        # stays frozen at whatever version existed when the job was first
        # registered, and (b) worse, on a script NAME change (e.g. #910's
        # evolution_access_gate.sh -> evolution_analysis_gate.sh) the
        # reconcile branch below only updates the job record's `script`
        # field — it does NOT install the file — so the job would end up
        # pointing at a script that was never copied into
        # HERMES_HOME/scripts/ at all. Installing here, unconditionally and
        # before that reconcile runs, covers both cases for every job kind.
        if str(spec.get("script") or "").strip() and not dry_run:
            _install_script(repo_root, str(spec["script"]).strip())

        schedule = _pick_schedule(spec, cadence, name, research_days)
        prompt = spec.get("prompt") or ""
        no_agent = bool(spec.get("no_agent"))
        if not schedule or (not str(prompt).strip() and not no_agent):
            failed.append((name, "missing required 'schedule' or 'prompt'"))
            continue
        if no_agent and not str(spec.get("script") or "").strip():
            failed.append((name, "no_agent job requires a 'script'"))
            continue
        if str(spec.get("model") or "").strip() or str(spec.get("provider") or "").strip():
            failed.append(
                (
                    name,
                    "model/provider are not supported for Evolution jobs; "
                    "AIAgent resolves the primary and fallback chain",
                )
            )
            continue

        skills = _normalize_skills(spec.get("skills"))
        toolsets = _normalize_toolsets(spec.get("toolsets"))
        deliver = str(spec.get("deliver") or "local").strip()
        # Refuse to register (or reconcile) an agent job whose skills need a
        # toolset the job definition does not grant — the scheduled job could
        # only ever silently no-op (#702). Jobs that DECLARE no toolsets are
        # exempt: enabled_toolsets stays None and the scheduler falls back to
        # the platform default toolset, which includes 'terminal'.
        if not no_agent and toolsets is not None:
            preflight_err = _validate_skill_toolsets(
                name, spec.get("skills"), toolsets, lint_mod, skill_texts, existing_scripts
            )
            if preflight_err:
                failed.append((name, f"toolset pre-flight: {preflight_err}"))
                continue

        # Existing job: reconcile mutable config from YAML instead of blindly
        # skipping. create_job() is idempotent-by-name, so without this an edit
        # to schedule/prompt/skills/toolsets in a *.yaml would NEVER take effect
        # on an already-registered job (the historical re-register gotcha — a
        # changed upstream-sync frequency, say, would silently stay on the old
        # schedule). We only touch jobs we own (everything here is evolution-*).
        if name in existing_names:
            cur = existing_jobs[name]
            if not cur.get("id"):
                # Malformed record without an id — cannot target an update.
                skipped.append(name)
                continue
            changes: dict = {}
            want_sched = parse_schedule(schedule).get("display", schedule)
            cur_sched = (cur.get("schedule") or {}).get("display") or cur.get(
                "schedule_display"
            )
            if want_sched != cur_sched:
                changes["schedule"] = schedule
            # EN: Routing fields never belong to an Evolution job, including
            # deterministic no_agent jobs. Clear legacy values before the
            # no_agent branch so every Evolution record has the same invariant.
            # UK: Поля маршруту не належать жодному Evolution-завданню, зокрема
            # детермінованим no_agent; очищаємо старі значення для всіх записів.
            for field in (
                "model",
                "provider",
                "model_snapshot",
                "provider_snapshot",
            ):
                if cur.get(field) is not None:
                    changes[field] = None
            if not no_agent:
                if str(prompt) != (cur.get("prompt") or ""):
                    changes["prompt"] = str(prompt)
                # skills/toolsets are None when the YAML omits them — that means
                # "leave the registered value as-is", NOT "clear it". Only
                # reconcile when the YAML explicitly specifies a value, and never
                # call list() on None: that TypeError silently aborted EVERY
                # re-register (and thus every integration self-update) once the
                # jobs already existed, freezing HERMES_HOME script/skill sync.
                if skills is not None and list(skills) != list(cur.get("skills") or []):
                    changes["skills"] = skills
                if toolsets is not None and list(toolsets) != list(
                    cur.get("enabled_toolsets") or []
                ):
                    changes["enabled_toolsets"] = toolsets
                # Detect script changes (e.g. Hydra replacing access gate)
                cur_script = str(cur.get("script") or "").strip()
                yaml_script = str(spec.get("script") or "").strip()
                want_script = yaml_script
                if not want_script:
                    if name == "evolution-issues":
                        want_script = issues_gate or ""
                    elif name == "evolution-introspection":
                        want_script = local_gate or ""
                    elif class_a:
                        want_script = gate_script or ""
                if want_script and want_script != cur_script:
                    changes["script"] = want_script
            if not changes:
                skipped.append(name)
            elif dry_run:
                updated.append((name, "DRY-RUN: " + ", ".join(sorted(changes))))
            else:
                update_job(cur["id"], changes)
                # EN: update_job recomputes snapshots when model/provider
                # changes. Clear them in a separate write so Evolution keeps
                # runtime routing fully under AIAgent. UK: update_job відновлює
                # відбитки після зміни маршруту, тому очищаємо їх окремим записом.
                if "model" in changes or "provider" in changes:
                    update_job(
                        cur["id"],
                        {"model_snapshot": None, "provider_snapshot": None},
                    )
                updated.append((name, ", ".join(sorted(changes))))
            continue

        if dry_run:
            created.append((name, "DRY-RUN"))
            existing_names.add(name)
            continue

        try:
            if no_agent:
                # Deterministic script job — no LLM agent at all. The script
                # itself IS the job; its stdout is delivered (empty = silent).
                script_name = str(spec["script"]).strip()
                installed = _install_script(repo_root, script_name)
                if not installed:
                    failed.append((name, f"could not install script {script_name}"))
                    continue
                create_kwargs = dict(
                    prompt=str(prompt) or name,
                    schedule=schedule,
                    name=name,
                    deliver=deliver,
                    no_agent=True,
                    script=installed,
                )
                job = create_job(**create_kwargs)
                created.append((name, job["id"]))
                existing_names.add(name)
                continue

            create_kwargs = dict(
                prompt=str(prompt),
                schedule=schedule,
                name=name,
                skills=skills,
                enabled_toolsets=toolsets,
                deliver=deliver,
            )
            # Does the YAML define its own script? (Hydra gate, etc.)
            yaml_script = str(spec.get("script") or "").strip() if not no_agent else None
            if yaml_script and not dry_run:
                _install_script(repo_root, yaml_script)
            if yaml_script:
                # Per-job gate script (Hydra, research web-gate, analysis, …)
                create_kwargs["script"] = yaml_script
            elif name == "evolution-issues" and issues_gate:
                create_kwargs["script"] = issues_gate
            elif name == "evolution-introspection" and local_gate:
                create_kwargs["script"] = local_gate
            elif class_a and gate_script:
                # Default GitHub-write gate for class-A jobs without their own
                # script. Class-B LLM jobs (introspection) use the local gate.
                create_kwargs["script"] = gate_script
            job = create_job(**create_kwargs)
            update_job(
                job["id"],
                {"model_snapshot": None, "provider_snapshot": None},
            )
            created.append((name, job["id"]))
            existing_names.add(name)
            if spec.get("enabled") is False:
                print(
                    f"[evolution-cron] note: '{name}' is enabled:false in YAML; "
                    f"created as enabled — pause it with 'hermes cron pause {job['id']}'"
                )
        except Exception as exc:
            failed.append((name, str(exc)))

    verb = "would register" if dry_run else "registered"
    print(
        f"[evolution-cron] {verb}={len(created)} reconciled={len(updated)} "
        f"skipped(unchanged)={len(skipped)} failed={len(failed)} "
        f"helper_scripts_installed={len(helper_scripts)} labels_ensured={len(label_ensured)}"
    )
    for name, jid in created:
        print(f"  + {name} ({jid})")
    for name, fields in updated:
        print(f"  ~ {name} (updated: {fields})")
    for name in skipped:
        print(f"  = {name} (unchanged)")
    for name, err in failed:
        print(f"  ! {name}: {err}")

    return 2 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
