"""Bounded, read-only evidence checks before a deferred plan has side effects.

The ledger records premises, never permission. Explicitly replacing ``decision``
replans a job; resume/run alone cannot acknowledge changed evidence. Legacy jobs
without a ledger retain their existing behavior.
"""
from __future__ import annotations

import hashlib
import math
import os
from pathlib import Path
import stat
from datetime import datetime, timezone
import uuid

from utils import atomic_json_write

MAX_WATCH_FILES = 16
MAX_EVIDENCE_BYTES = 1024 * 1024


def _now():
    return datetime.now(timezone.utc)


def _evidence_path(path: str, workdir=None) -> Path:
    from hermes_constants import get_hermes_home, named_profile_home
    home = get_hermes_home().resolve()
    resolved = Path(path).resolve(strict=True)
    profile = named_profile_home(resolved)
    if profile is not None and profile.resolve() != home:
        raise ValueError("watch evidence belongs to another profile")
    roots = [home]
    if workdir:
        roots.append(Path(workdir).resolve(strict=True))
    if not any(resolved.is_relative_to(root) for root in roots):
        raise ValueError("watch evidence must be inside the active profile or job workdir")
    # Default-profile scope must not include its child profiles.
    if resolved.is_relative_to(home / "profiles"):
        raise ValueError("watch evidence belongs to another profile")
    return resolved


def _digest(path: str, workdir=None) -> str:
    # No commands, network probes, devices or unbounded file reads.
    path = _evidence_path(path, workdir)
    # The capability belongs to the platform's open, not to whatever wrapper this
    # process installed: the test harness' home-I/O guard wraps os.open with
    # functools.wraps, which would otherwise hide dir_fd support and turn every
    # digest into "unsupported on this platform".
    _opener = getattr(os.open, "__wrapped__", os.open)
    if _opener not in os.supports_dir_fd or not hasattr(os, "O_NOFOLLOW"):
        raise ValueError("descriptor-scoped watch files are unsupported on this platform")
    # Open EVERY component relative to an already-open directory. A concurrent
    # parent swap cannot turn the validated path into an out-of-scope symlink.
    parent = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for component in path.parts[1:-1]:
            child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
            os.close(parent)
            parent = child
        fd = os.open(path.name, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW, dir_fd=parent)
    finally:
        os.close(parent)
    with os.fdopen(fd, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError("watch evidence must be a regular file")
        data = stream.read(MAX_EVIDENCE_BYTES + 1)
    if len(data) > MAX_EVIDENCE_BYTES:
        raise ValueError("watch evidence exceeds 1 MiB")
    return hashlib.sha256(data).hexdigest()


def _ceiling(value):
    if value is not None and (isinstance(value, bool) or not isinstance(value, (float, int))
                              or not math.isfinite(value) or value <= 0):
        raise ValueError("max_age_seconds must be a finite positive number")
    return value


def capture(spec=None, *, workdir=None) -> dict:
    """Capture actual evidence, not caller-supplied hashes or acceptance flags."""
    spec = {} if spec is None else spec
    if not isinstance(spec, dict) or set(spec) - {"watch_files", "max_age_seconds"}:
        raise ValueError("decision accepts only watch_files and max_age_seconds")
    paths = spec.get("watch_files", [])
    if not isinstance(paths, list) or len(paths) > MAX_WATCH_FILES:
        raise ValueError("watch_files must be a list of at most 16 absolute file paths")
    ceiling = _ceiling(spec.get("max_age_seconds"))
    observed = _now().isoformat()
    evidence = []
    for path in paths:
        if not isinstance(path, str) or not Path(path).is_absolute():
            raise ValueError("watch_files must contain absolute file paths")
        evidence.append({"path": path, "sha256": _digest(path, workdir), "observed_at": observed})
    return {"id": uuid.uuid4().hex, "created_at": observed, "state": "active",
            "max_age_seconds": ceiling, "watch_conditions": evidence}


def check(ledger, *, workdir=None) -> str | None:
    """Return a reopening reason on changed, stale, unavailable or invalid evidence."""
    try:
        if not isinstance(ledger, dict) or ledger.get("state") != "active":
            return "decision is reopened or malformed; explicitly replan before running"
        ceiling = _ceiling(ledger["max_age_seconds"])
        conditions = ledger["watch_conditions"]
        if not isinstance(conditions, list) or len(conditions) > MAX_WATCH_FILES:
            raise ValueError("invalid watch conditions")
        now = _now()
        timestamps = [ledger["created_at"]] + [e["observed_at"] for e in conditions]
        for stamp in timestamps:
            age = (now - datetime.fromisoformat(stamp)).total_seconds()
            if age < 0 or (ceiling is not None and age > ceiling):
                return "decision evidence exceeded its freshness ceiling"
        for evidence in conditions:
            path = evidence["path"]
            if not isinstance(path, str) or not Path(path).is_absolute():
                raise ValueError("invalid evidence path")
            if _digest(path, workdir) != evidence["sha256"]:
                return "a watched file changed"
        return None
    except (OSError, ValueError, TypeError, KeyError, OverflowError):
        return "decision evidence is unreadable or malformed"


def preflight(job: dict) -> str | None:
    """Fail closed before scripts/monitors/agents; persist the reopened state."""
    if "decision_ledger" not in job:
        return None
    reason = check(job["decision_ledger"], workdir=job.get("workdir"))
    if reason is None:
        return None
    from cron import jobs
    from hermes_constants import get_hermes_home

    def reopen(records, index, current):
        # Never overwrite a newer replan with this execution's stale snapshot.
        if current.get("decision_ledger") != job.get("decision_ledger"):
            return
        ledger = current.get("decision_ledger")
        if not isinstance(ledger, dict):
            ledger = {}
        already_reopened = ledger.get("state") == "reopened"
        ledger = {**ledger, "state": "reopened", "reopened_at": _now().isoformat(), "reason": reason}
        current.update(decision_ledger=ledger, enabled=False, state="paused",
                       paused_at=ledger["reopened_at"], paused_reason=reason, next_run_at=None)
        claim = current.pop("decision_repeat_claim", None)
        repeat = current.get("repeat") or {}
        if (not already_reopened and isinstance(claim, dict)
                and claim.get("decision_id") == ledger.get("id")
                and claim.get("completed") == repeat.get("completed")
                and repeat.get("completed", 0) > 0):
            repeat["completed"] -= 1  # only the claim made for THIS decision
        records[index] = current
        jobs.save_jobs(records)
        if not already_reopened:
            # One immutable local event per reopen, consumed by compute_funnel.
            atomic_json_write(get_hermes_home() / "evolution" / "decision-reopens" / f"{uuid.uuid4().hex}.json",
                              {"job_id": job["id"], "decision_id": ledger.get("id"),
                               "created_at": ledger["reopened_at"], "reason": reason})

    try:
        jobs._with_job(job["id"], reopen)
    except Exception:
        # Failure to save a pause/event must NEVER permit the stale action.
        import logging
        logging.getLogger(__name__).exception("Could not persist reopened decision for %s", job["id"])
    return f"Decision reopened: {reason}. Review the premises, update decision, then resume."
