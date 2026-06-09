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

import sys
from pathlib import Path


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


def _install_access_gate(repo_root: Path) -> str | None:
    """Copy the GitHub access wake-gate into HERMES_HOME/scripts so cron runs it
    as a pre-check before each evolution job. Returns the script name to attach
    to every job, or None on failure (jobs are still created, just ungated).

    The gate prints ``{"wakeAgent": false}`` when GitHub is unreachable, which
    makes the scheduler SKIP the LLM agent entirely — no tokens / web-search
    spent when the cycle has no outlet to post issues/PRs.
    """
    import os
    import shutil

    src = repo_root / "scripts" / "evolution_access_gate.sh"
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
        print(f"[evolution-cron] warning: could not install access gate: {exc}", file=sys.stderr)
        return None


def main(argv: list[str]) -> int:
    dry_run = "--dry-run" in argv
    positional = [a for a in argv[1:] if not a.startswith("--")]

    repo_root = Path(__file__).resolve().parent.parent
    src_dir = Path(positional[0]) if positional else repo_root / "cron" / "evolution"
    if not src_dir.is_dir():
        print(f"[evolution-cron] no evolution cron dir at {src_dir}", file=sys.stderr)
        return 1

    # Import the canonical Hermes cron API (writes ~/.hermes/cron/jobs.json).
    sys.path.insert(0, str(repo_root))
    try:
        from cron.jobs import create_job, load_jobs
    except Exception as exc:  # pragma: no cover - environment dependent
        print(f"[evolution-cron] cannot import cron.jobs: {exc}", file=sys.stderr)
        return 1

    # Install the GitHub-access wake-gate and attach it to every evolution job,
    # so the expensive LLM agent only runs when GitHub is actually reachable.
    gate_script = None if dry_run else _install_access_gate(repo_root)

    existing_names = {str(j.get("name", "")).strip() for j in load_jobs()}
    created: list[tuple[str, str]] = []
    skipped: list[str] = []
    failed: list[tuple[str, str]] = []

    for yaml_file in sorted(src_dir.glob("*.yaml")):
        try:
            spec = _load_yaml(yaml_file)
        except Exception as exc:
            failed.append((yaml_file.name, f"yaml parse error: {exc}"))
            continue

        name = str(spec.get("name") or yaml_file.stem).strip()
        if name in existing_names:
            skipped.append(name)
            continue

        schedule = str(spec.get("schedule") or "").strip()
        prompt = spec.get("prompt") or ""
        if not schedule or not str(prompt).strip():
            failed.append((name, "missing required 'schedule' or 'prompt'"))
            continue

        skills = _normalize_skills(spec.get("skills"))
        toolsets = spec.get("toolsets") or None
        if isinstance(toolsets, str):
            toolsets = [toolsets]

        if dry_run:
            created.append((name, "DRY-RUN"))
            existing_names.add(name)
            continue

        try:
            create_kwargs = dict(
                prompt=str(prompt),
                schedule=schedule,
                name=name,
                skills=skills,
                enabled_toolsets=list(toolsets) if toolsets else None,
                deliver="local",
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
        f"[evolution-cron] {verb}={len(created)} "
        f"skipped(existing)={len(skipped)} failed={len(failed)}"
    )
    for name, jid in created:
        print(f"  + {name} ({jid})")
    for name in skipped:
        print(f"  = {name} (already registered)")
    for name, err in failed:
        print(f"  ! {name}: {err}")

    return 2 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
