"""Profile-local cron snapshot drift policy and operator impact notices."""
import os
import unicodedata
from typing import Any, Optional

# ---- Cron model-drift helpers: which unpinned jobs stay on their creation snapshot ----

_CRON_DRIFT_AXIS_BY_KEY = {
    "model": "model", "model.default": "model", "model.model": "model", "model.name": "model",
    "model.provider": "provider", "provider": "provider"}


def _cron_model_drift_axis_for_config_key(key: str) -> Optional[str]:
    """Return the cron inference axis affected by a config key, if any."""
    return _CRON_DRIFT_AXIS_BY_KEY.get(str(key or "").strip().lower())


def _cron_section(config: Optional[dict[str, Any]]) -> Optional[dict[str, Any]]:
    """Return the ``cron`` mapping of *config* (loading the merged config when None), else None."""
    if config is None:
        try:
            from hermes_cli.config import load_config
            config = load_config()
        except Exception:
            return None
    cron_config = config.get("cron") if isinstance(config, dict) else None
    return cron_config if isinstance(cron_config, dict) else None


def cron_model_drift_guard_enabled(
    config: Optional[dict[str, Any]] = None,
) -> bool:
    """Return whether cron must fail closed on unpinned inference drift.

    Only the literal YAML boolean ``false`` disables this spend-safety guard.
    Missing, malformed, or non-boolean values stay fail-closed. When *config*
    is omitted, load the active merged configuration so CLI warnings honor the
    same user/managed setting as the scheduler.
    """
    if config is None:
        try:
            from hermes_cli.config import load_config
            config = load_config()
        except Exception:
            return True
    if not isinstance(config, dict):
        return True
    cron_config = config.get("cron")
    if not isinstance(cron_config, dict):
        return True
    return cron_config.get("model_drift_guard", True) is not False


_CRON_MODEL_IMPACT_JOB_LIMIT = 50
_CRON_MODEL_IMPACT_ID_LIMIT = 256
_CRON_MODEL_IMPACT_NAME_LIMIT = 120


def _model_assignment_text(value: Any) -> str:
    """Return a trimmed scalar model/provider value, or empty for malformed data."""
    return value.strip() if isinstance(value, str) else ""


def resolve_cron_model_drift_defaults(
    config: Any, *, environ: Optional[dict[str, str]] = None) -> tuple[str, str]:
    """Resolve the global ``(provider, model)`` cron compares against snapshots.
    Mirrors the scheduler's precedence: a truthy configured model wins over ``HERMES_MODEL``; the
    environment is only a fallback. Per-job and cron fleet defaults are handled by the caller
    because they cover an axis rather than changing the global assignment."""
    env = os.environ if environ is None else environ
    provider = ""
    model_config = config.get("model") if isinstance(config, dict) else None
    if isinstance(model_config, dict):
        provider = _model_assignment_text(model_config.get("provider"))
        model_config = model_config.get("default") or model_config.get("model") or model_config.get("name")
    configured_model = _model_assignment_text(model_config)
    return provider, configured_model or _model_assignment_text(env.get("HERMES_MODEL", ""))


def cron_model_drift_axes(
    job: Any, *, current_provider: Any = "", current_model: Any = "", config: Any = None
) -> list[str]:
    """Return the unpinned axes on which *job* will keep running on its creation snapshot rather
    than the new global assignment (the scheduler treats the snapshot as the effective pin)."""
    from hermes_cli.config import cron_model_drift_guard_enabled
    if not isinstance(job, dict) or not cron_model_drift_guard_enabled(config):
        return []

    current = {
        "provider": _model_assignment_text(current_provider).lower(),
        "model": _model_assignment_text(current_model).lower()}
    # A cron.model / cron.model_provider fleet default covers its axis: that axis never reads the
    # snapshot at fire time, so reporting it would be false.
    fleet = _cron_section(config) or {}
    drifted: list[str] = []
    for axis, fleet_key in (("provider", "model_provider"), ("model", "model")):
        if _model_assignment_text(fleet.get(fleet_key)) or _model_assignment_text(job.get(axis)):
            continue
        snapshot = _model_assignment_text(job.get(f"{axis}_snapshot")).lower()
        if snapshot and current[axis] and snapshot != current[axis]:
            drifted.append(axis)
    return drifted


def _is_control_char(char: str) -> bool:
    return unicodedata.category(char).startswith("C")


def _valid_cron_impact_job_id(value: Any) -> str:
    job_id = value.strip() if isinstance(value, str) else ""
    if len(job_id) > _CRON_MODEL_IMPACT_ID_LIMIT or any(map(_is_control_char, job_id)):
        return ""
    return job_id


def _cron_impact_job_name(value: Any, job_id: str) -> str:
    if isinstance(value, str):
        printable = "".join(char for char in value if not _is_control_char(char))
        name = " ".join(printable.split())[:_CRON_MODEL_IMPACT_NAME_LIMIT].rstrip()
        if name:
            return name
    return f"Job {job_id}"[:_CRON_MODEL_IMPACT_NAME_LIMIT].rstrip()


def _cron_model_impact_result(available: bool) -> dict[str, Any]:
    return {"available": available, "affected_count": 0, "truncated": False, "jobs": []}


def build_cron_model_impact(
    *, current_provider: Any = "", current_model: Any = "", config: Any = None, jobs: Any = None
) -> dict[str, Any]:
    """Build a bounded, profile-local summary of unpinned jobs that stay on their creation snapshot
    after a global model/provider change. Job-store inspection is best effort: the model assignment
    has already succeeded when Desktop requests this, so an unreadable store is reported as
    unavailable rather than failing."""
    from hermes_cli.config import cron_model_drift_axes
    if jobs is None:
        try:
            from cron.jobs import load_jobs

            jobs = load_jobs()
        except Exception:
            return _cron_model_impact_result(False)
    if not isinstance(jobs, list):
        return _cron_model_impact_result(False)

    result = _cron_model_impact_result(True)

    from cron.jobs import is_job_runnable

    seen_ids: set[str] = set()
    for job in jobs:
        if not isinstance(job, dict) or not is_job_runnable(job) or job.get("no_agent"):
            continue
        job_id = _valid_cron_impact_job_id(job.get("id"))
        if not job_id or job_id in seen_ids:
            continue
        seen_ids.add(job_id)
        axes = cron_model_drift_axes(
            job, current_provider=current_provider, current_model=current_model, config=config)
        if not axes:
            continue
        result["affected_count"] += 1
        if len(result["jobs"]) < _CRON_MODEL_IMPACT_JOB_LIMIT:
            result["jobs"].append({
                "id": job_id,
                "name": _cron_impact_job_name(job.get("name"), job_id),
                "drifted_axes": axes})

    result["truncated"] = result["affected_count"] > len(result["jobs"])
    return result


def warn_unpinned_cron_jobs_after_model_config_change(
    key: str, value: Any, config: Optional[dict[str, Any]] = None) -> None:
    """Tell the operator which unpinned cron jobs a global model/provider change does NOT move."""
    from hermes_cli.config import build_cron_model_impact
    axis = _cron_model_drift_axis_for_config_key(key)
    if axis is None:
        return

    new_value = _model_assignment_text(value)
    if not new_value:
        return
    impact = build_cron_model_impact(
        current_provider=new_value if axis == "provider" else "",
        current_model=new_value if axis == "model" else "", config=config, jobs=None)
    affected = impact["affected_count"]
    if affected <= 0:
        return

    noun, verb = ("job", "keeps") if affected == 1 else ("jobs", "keep")
    print(
        f"ℹ️  {affected} unpinned cron {noun} {verb} running on the {axis} it was created under "
        f"(its {axis}_snapshot), not the new global {axis}. To move it, pin it with "
        "`hermes cron edit <job_id> --provider <provider> --model <model>` or set a fleet default "
        "with `hermes config set cron.model <model>`.")
