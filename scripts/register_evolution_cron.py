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
import sys
from pathlib import Path


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
    import yaml  # PyYAML ships with Hermes (used for cli-config.yaml etc.)

    with open(path, "r", encoding="utf-8") as fh:
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
    scripts are picked up automatically by the glob."""
    installed: list[str] = []
    for src in sorted((repo_root / "scripts").glob("evolution_*.py")):
        if _install_script(repo_root, src.name):
            installed.append(src.name)
    return installed


def main(argv: list[str]) -> int:
    dry_run = "--dry-run" in argv
    positional = [a for a in argv[1:] if not a.startswith("--")]

    repo_root = Path(__file__).resolve().parent.parent

    # Before importing ANY Hermes module, make sure we're on the venv python
    # that actually has the dependencies (dotenv, croniter, …). This replaces
    # the process when needed, so nobody has to launch us with the right python.
    _ensure_venv_python(repo_root, argv)

    src_dir = Path(positional[0]) if positional else repo_root / "cron" / "evolution"
    if not src_dir.is_dir():
        print(f"[evolution-cron] no evolution cron dir at {src_dir}", file=sys.stderr)
        return 1

    # Import the canonical Hermes cron API (writes ~/.hermes/cron/jobs.json).
    sys.path.insert(0, str(repo_root))
    try:
        from cron.jobs import create_job, load_jobs, parse_schedule, update_job
    except Exception as exc:  # pragma: no cover - environment dependent
        print(f"[evolution-cron] cannot import cron.jobs: {exc}", file=sys.stderr)
        return 1

    # Install the GitHub-access wake-gate and attach it to every evolution job,
    # so the expensive LLM agent only runs when GitHub is actually reachable.
    gate_script = None if dry_run else _install_access_gate(repo_root)

    # Install the whole evolution_* helper family so no_agent scripts' sibling
    # imports (funnel -> metrics/realized_impact) resolve in HERMES_HOME/scripts.
    helper_scripts = [] if dry_run else _install_evolution_helpers(repo_root)

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

        # Refresh installed no_agent scripts on EVERY run — including for
        # already-registered jobs — mirroring the access gate above:
        # `hermes update` refreshes the repo checkout, but the scheduler
        # executes the copy in HERMES_HOME/scripts; without this refresh the
        # installed script stays frozen at whatever version existed when the
        # job was first registered.
        if spec.get("no_agent") and str(spec.get("script") or "").strip() and not dry_run:
            _install_script(repo_root, str(spec["script"]).strip())

        schedule = str(spec.get("schedule") or "").strip()
        prompt = spec.get("prompt") or ""
        no_agent = bool(spec.get("no_agent"))
        if not schedule or (not str(prompt).strip() and not no_agent):
            failed.append((name, "missing required 'schedule' or 'prompt'"))
            continue
        if no_agent and not str(spec.get("script") or "").strip():
            failed.append((name, "no_agent job requires a 'script'"))
            continue

        skills = _normalize_skills(spec.get("skills"))
        toolsets = _normalize_toolsets(spec.get("toolsets"))
        deliver = str(spec.get("deliver") or "local").strip()

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
            cur_sched = (cur.get("schedule") or {}).get("display") or cur.get("schedule_display")
            if want_sched != cur_sched:
                changes["schedule"] = schedule
            if not no_agent:
                if str(prompt) != (cur.get("prompt") or ""):
                    changes["prompt"] = str(prompt)
                if list(skills) != list(cur.get("skills") or []):
                    changes["skills"] = skills
                if list(toolsets) != list(cur.get("enabled_toolsets") or []):
                    changes["enabled_toolsets"] = toolsets
            if not changes:
                skipped.append(name)
            elif dry_run:
                updated.append((name, "DRY-RUN: " + ", ".join(sorted(changes))))
            else:
                update_job(cur["id"], changes)
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
            if gate_script:
                # Pre-check script: skips the agent (no LLM/web spend) when
                # GitHub is unreachable. Keeps the LLM agent (skills) for the run.
                create_kwargs["script"] = gate_script
            job = create_job(**create_kwargs)
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
        f"helper_scripts_installed={len(helper_scripts)}"
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
