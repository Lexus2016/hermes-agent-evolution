"""Evolution cron jobs must never share a firing minute (issue #1673).

The pipeline's LLM-hitting stages all called the same provider on the hour, so
concurrent runs collided into ~90 HTTP 429s/day from zai/glm-5.2, clustered at
hour boundaries. PR #1678 staggered most stages onto distinct minute offsets;
this test is the guard that keeps them that way, and it checks the invariant
itself rather than a list of hard-coded offsets — a per-job assertion goes stale
the moment a schedule legitimately moves, and says nothing about the pair that
actually collides.

What it computes: the full (hour, minute) firing set for every schedule, then
every pairwise intersection. Two jobs may share an hour freely; what they must
not share is a minute, because that is when their API calls overlap.
"""

from __future__ import annotations

import itertools
import re
from pathlib import Path

import pytest

CRON_DIR = Path(__file__).resolve().parents[2] / "cron" / "evolution"


def _schedule(path: Path) -> str | None:
    match = re.search(r'^schedule:\s*"([^"]+)"', path.read_text(encoding="utf-8"), re.M)
    return match.group(1) if match else None


def _expand(field: str, lo: int, hi: int) -> set[int]:
    """Expand one cron field (``*``, ``a,b``, ``a-b``, ``*/n``) to its values."""
    if field == "*":
        return set(range(lo, hi + 1))
    out: set[int] = set()
    for part in field.split(","):
        if part.startswith("*/"):
            out |= set(range(lo, hi + 1, int(part[2:])))
        elif "-" in part:
            start, end = part.split("-")
            out |= set(range(int(start), int(end) + 1))
        else:
            out.add(int(part))
    return out


def _firing_slots(schedule: str) -> set[tuple[int, int]]:
    """Every (hour, minute) this schedule fires at, within a day."""
    minute_field, hour_field = schedule.split()[0], schedule.split()[1]
    return {
        (hour, minute)
        for hour in _expand(hour_field, 0, 23)
        for minute in _expand(minute_field, 0, 59)
    }


def _all_jobs() -> dict[str, str]:
    jobs = {}
    for path in sorted(CRON_DIR.glob("*.yaml")):
        schedule = _schedule(path)
        if schedule:
            jobs[path.stem] = schedule
    return jobs


def test_cron_dir_is_discoverable():
    """Guard the guard: a wrong CRON_DIR would make every check below vacuous."""
    jobs = _all_jobs()
    assert len(jobs) >= 10, f"expected the evolution pipeline, found {sorted(jobs)}"


def test_no_two_evolution_jobs_share_a_firing_minute():
    """The #1673 invariant: no two jobs may fire in the same wall-clock minute."""
    slots = {name: _firing_slots(sched) for name, sched in _all_jobs().items()}

    collisions = []
    for left, right in itertools.combinations(sorted(slots), 2):
        shared = slots[left] & slots[right]
        if shared:
            when = ", ".join(f"{h:02d}:{m:02d}" for h, m in sorted(shared)[:4])
            more = "" if len(shared) <= 4 else f" (+{len(shared) - 4} more)"
            collisions.append(f"{left} × {right} at {when}{more}")

    assert not collisions, (
        "Evolution cron jobs fire in the same minute — concurrent provider "
        "calls are what caused the HTTP 429 stampedes in #1673:\n  "
        + "\n  ".join(collisions)
        + "\nMove one of each pair to a free minute offset."
    )


@pytest.mark.parametrize("name", sorted(_all_jobs()))
def test_schedule_is_a_five_field_cron_expression(name: str):
    """A malformed schedule would silently drop out of the collision check."""
    fields = _all_jobs()[name].split()
    assert len(fields) == 5, f"{name}: expected 5 cron fields, got {fields}"
