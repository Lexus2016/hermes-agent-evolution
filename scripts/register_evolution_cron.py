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

Per-stage model allocation (#905)
----------------------------------
A stage YAML may set optional ``model:`` / ``provider:`` keys to pin that
stage to a specific model — e.g. a cheaper/mid-tier model for the broad,
capability-flat research/analysis stages, leaving implementation on the
deployment's main/frontier default. Both are independent and optional;
omitting either leaves that axis unpinned (follows the global config
default, same as today). This is a *static per-stage* pin, distinct from
the *dynamic per-subtask* complexity routing added by #798
(``evolution_draft_selector.route_cost_tier`` / ``model_hint`` on delegated
worker tasks) — the two do not overlap.

Caveat: ``model`` and ``provider`` reconcile independently (each only
updates when its own YAML value differs from the stored one), matching the
pre-existing skills/toolsets pattern below. cron.jobs.create_job/update_job
already allow pinning either axis alone, with no cross-validation between
them — that is inherited, not introduced here. If a stage's model and
provider are both pinned, always edit both together when changing either;
an edit that changes only one of the two while the job already has a
mismatched value for the other risks an invalid model/provider combination
at run time. The shipped stage YAMLs avoid this by leaving both commented
out as a single paired block (see research.yaml / analysis.yaml).

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
            if result.returncode == 0:
                created.append(name)
            elif "already exists" in (result.stderr or "").lower():
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
        # Per-stage model allocation (#905): an evolution stage may pin a
        # cheaper/mid-tier model (research, analysis) while leaving others
        # (implementation) on the deployment's main/frontier default. Both
        # keys are optional and independent — omitting one leaves that axis
        # unpinned (follows global config, same as today). Values pass
        # through to cron.jobs.create_job/update_job unchanged; that layer
        # already validates/normalizes and (#44585) drift-guards unpinned
        # jobs against a later global default change.
        model = str(spec.get("model") or "").strip() or None
        provider = str(spec.get("provider") or "").strip() or None

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
                # model/provider (#905): same "None means leave as-is" rule as
                # skills/toolsets above — a YAML that doesn't mention model:/
                # provider: must never clear an already-pinned job back to
                # unpinned. The two reconcile independently (see module
                # docstring caveat) — always edit both together in the YAML.
                if model is not None and model != cur.get("model"):
                    changes["model"] = model
                if provider is not None and provider != cur.get("provider"):
                    changes["provider"] = provider
                # Detect script changes (e.g. Hydra replacing access gate)
                cur_script = str(cur.get("script") or "").strip()
                yaml_script = str(spec.get("script") or "").strip()
                if yaml_script and yaml_script != cur_script:
                    changes["script"] = yaml_script
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
                model=model,
                provider=provider,
            )
            # Does the YAML define its own script? (Hydra gate, etc.)
            yaml_script = str(spec.get("script") or "").strip() if not no_agent else None
            if yaml_script and not dry_run:
                _install_script(repo_root, yaml_script)
            if gate_script and not yaml_script:
                # Default access gate: skips the agent (no LLM/web spend) when
                # GitHub is unreachable. Jobs with their own script (e.g. the
                # Hydra gate) manage their own pre-checks.
                create_kwargs["script"] = gate_script
            elif yaml_script:
                # Per-job gate script (Hydra, etc.) — installed and attached.
                create_kwargs["script"] = yaml_script
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

    # Config-drift validation (#938): warn when an agent-stage job has BOTH
    # model and provider unpinned (both None), meaning it inherits the global
    # inference config and is vulnerable to config drift that caused a mass
    # blackout in July 2026. no_agent jobs (deterministic scripts) do not use
    # model/provider and are excluded from this check.
    def _is_unpinned_yaml(yaml_path: Path) -> bool | None:
        """Check if a YAML file defines an unpinned agent job.
        Returns True if unpinned, False if pinned, None if no_agent or not found."""
        if not yaml_path.exists():
            return None
        try:
            spec = _load_yaml(yaml_path)
        except Exception:
            return None
        if bool(spec.get("no_agent")):
            return None
        yaml_model = str(spec.get("model") or "").strip() or None
        yaml_provider = str(spec.get("provider") or "").strip() or None
        return yaml_model is None and yaml_provider is None

    def _check_unpinned(name: str, src_dir: Path) -> bool:
        """Check if a named job is unpinned in its YAML definition."""
        stem = name.replace("evolution-", "")
        yaml_path = src_dir / f"{stem}.yaml"
        result = _is_unpinned_yaml(yaml_path)
        if result is True:
            return True
        if result is None and not yaml_path.exists():
            # Fallback: search all YAMLs
            for candidate in sorted(src_dir.glob("*.yaml")):
                r = _is_unpinned_yaml(candidate)
                if r is not None:
                    return r
        return False

    unpinned: list[str] = []
    all_processed: list[str] = []
    all_processed.extend(n for n, _ in created)
    all_processed.extend(n for n, _ in updated)
    all_processed.extend(skipped)  # skipped is list[str]
    for name in all_processed:
        if _check_unpinned(name, src_dir):
            unpinned.append(name)

    if unpinned:
        print(
            "[evolution-cron] warning: the following agent jobs have unpinned "
            "model AND provider — they are vulnerable to global inference config "
            "drift. Set model:/provider: in their YAML to pin them to a specific "
            f"deployment model (see #938).\n"
            f"  unpinned: {', '.join(sorted(set(unpinned)))}",
            file=sys.stderr,
        )

    return 2 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
