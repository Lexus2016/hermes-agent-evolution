#!/usr/bin/env python3
"""Evolution watchdog — deterministic pipeline health check (issue 83).

Runs as a ``no_agent`` cron job (no LLM involved): scans the evolution
pipeline's observable state and prints a short alert report to stdout when
something is wrong. The cron scheduler delivers non-empty stdout to the
owner's configured messaging channels; EMPTY stdout means "all healthy"
and nothing is delivered (silent run).

Checks
------
1. Stage reports — every daily evolution stage must have left a non-trivial
   report file for its most recent *expected* slot (slot + grace period).
   This catches jobs that were killed mid-run without any record (the
   2026-06-10 gateway-restart incident) and jobs that finished "ok" while
   producing nothing.
2. Job registry health — evolution jobs in ``cron/jobs.json``: last run
   status is not "error", the job actually ran within its cadence window,
   and it is not stuck in a running state for hours.
3. GitHub access — ``gh auth status`` works and the API rate limit isn't
   nearly exhausted (every pipeline stage depends on gh).

Designed to be import-safe for tests: all checks are pure functions taking
explicit paths / clocks / runners.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Dict, List, Tuple

# stage name -> (daily slot hour, report file extension)
# Slots and extensions mirror cron/evolution/*.yaml (schedule + output.file).
# Drift is locked down by TestStagesMirrorCronSpecs in
# tests/scripts/test_evolution_watchdog.py.
STAGES: Dict[str, Tuple[int, str]] = {
    "research": (9, "md"),
    "introspection": (20, "json"),
    # analysis/implementation/integration run every 4h (processing throughput);
    # the slot here is the FIRST daily slot — the watchdog only needs "a report
    # for today exists by then", and reports are date-keyed (overwritten each
    # run). Mirrors cron/evolution/*.yaml first hour (locked by the mirror test).
    "analysis": (1, "json"),
    "implementation": (2, "md"),
    "integration": (3, "json"),
}

GRACE_HOURS = 2
MIN_REPORT_BYTES = 50
DAILY_STALE_HOURS = 26
WEEKLY_STALE_HOURS = 8 * 24
STUCK_RUNNING_HOURS = 12
MIN_GH_RATE_REMAINING = 200
# RELEASE-tracking model (2026-07-01): the fork mirrors upstream's PUBLISHED
# releases, not bleeding-edge ``upstream/main``. Upstream lands ~300 commits/day —
# chasing every one is an unwinnable tax for a heavily-customized fork, and every
# daily wholesale ``git merge upstream/main`` exceeded the escalation ceiling and
# stalled. So the fork tracks upstream RELEASE tags (~weekly/biweekly) and THIS
# check alerts only when a published upstream release has not been merged within a
# grace window. Raw "behind upstream/main" (unreleased churn) is expected now and
# is NOT an anomaly — alerting on it was a permanent false alarm under this model.
UPSTREAM_RELEASE_GRACE_HOURS = 36  # one+ daily release-sync cycle to absorb a new tag

# Title prefix of the GitHub tracking issue the release-sync escalates to when it
# can no longer land a release cleanly. The watchdog re-files this idempotently on
# a real escalation so the owner never has to open it by hand (issue #562 was
# opened manually). An OPEN issue carrying this prefix is the idempotency key —
# its presence blocks creation of a duplicate.
UPSTREAM_ISSUE_PREFIX = "[UPSTREAM]"
# Toggle the gh issue-filing side effect. Default on; flip to "0" to fall back to
# text-only (e.g. CI, or a box where gh isn't authed). Filing is ALWAYS fail-open
# regardless of this flag — a missing/unauthed gh never crashes the watchdog.
UPSTREAM_ISSUE_ENABLED = os.environ.get("WATCHDOG_FILE_UPSTREAM_ISSUE", "1") != "0"

# Jobs that are weekly, not daily (looser staleness threshold).
WEEKLY_JOBS = {"evolution-upstream-sync"}
# The watchdog itself must not alert about its own first run.
SELF_NAMES = {"evolution-watchdog"}

# Edge-triggering for the steady-state HEALTH alerts ------------------------
# Re-reminder cadence: a health condition that persists UNCHANGED for at least
# this many days is re-announced once (a "still unresolved" nudge) so a real
# fault can never be silenced forever by suppression. The clock resets on every
# actual emission (first sighting, transition, or a prior re-reminder).
EDGE_COOLDOWN_DAYS = 7
# State file lives beside the health sidecars (same evolution_dir resolution the
# health checks already use), so a single EVOLUTION_PROFILE_DIR override moves
# both. Small JSON: {"signature": str, "last_emitted_at": ISO8601}.
ALERT_STATE_FILENAME = "watchdog-alert-state.json"


def expected_report_date(
    now: datetime, slot_hour: int, grace_hours: int = GRACE_HOURS
) -> str:
    """Date (YYYY-MM-DD) whose report should exist for a daily slot.

    If the slot (plus grace) has already passed today, today's report is
    expected; otherwise yesterday's is the most recent one that must exist.
    """
    slot_deadline = now.replace(
        hour=slot_hour, minute=0, second=0, microsecond=0
    ) + timedelta(hours=grace_hours)
    day = now.date() if now >= slot_deadline else (now - timedelta(days=1)).date()
    return day.isoformat()


def _load_jobs(jobs_file: Path | None) -> List[dict]:
    if jobs_file is None:
        return []
    try:
        data = json.loads(jobs_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return data.get("jobs", data if isinstance(data, list) else [])


def _stage_clean_job_for_slot(
    jobs: List[dict], stage: str, date: str, slot_hour: int
) -> dict | None:
    """The stage's cron job record when it ran with ``last_status == "ok"`` at
    or after its slot on ``date`` — else None. A MISSING report is only a death
    if the job did NOT run clean for the slot: when it did, the stage executed
    and simply had nothing to do (e.g. analysis selected 0 →
    implementation/integration are legitimately idle and need not emit a
    report). That is a clean idle cycle, not 'the job died without record'.
    Callers still inspect the returned record: a "clean" run with ZERO tool
    calls could not have done any real work (#701)."""
    name = f"evolution-{stage}"
    for job in jobs:
        if str(job.get("name", "")) != name:
            continue
        if job.get("last_status") != "ok":
            return None
        last_run = _parse_iso(job.get("last_run_at"))
        if last_run is None:
            return None
        try:
            slot_dt = datetime.fromisoformat(date).replace(hour=slot_hour)
        except ValueError:
            return None
        return job if last_run >= slot_dt else None
    return None


def _stage_ran_clean_for_slot(
    jobs: List[dict], stage: str, date: str, slot_hour: int
) -> bool:
    """Boolean wrapper kept for existing callers — see
    ``_stage_clean_job_for_slot``."""
    return _stage_clean_job_for_slot(jobs, stage, date, slot_hour) is not None


def check_stage_reports(
    evolution_dir: Path, now: datetime, jobs_file: Path | None = None
) -> List[str]:
    """Alert for every stage whose expected report is missing or trivial.

    A missing report is suppressed (not a death) when the stage's cron job ran
    clean for the slot — see ``_stage_ran_clean_for_slot``."""
    alerts: List[str] = []
    jobs = _load_jobs(jobs_file)
    for stage, (slot_hour, ext) in STAGES.items():
        date = expected_report_date(now, slot_hour)
        report = evolution_dir / stage / f"{date}.{ext}"
        if not report.exists():
            clean_job = _stage_clean_job_for_slot(jobs, stage, date, slot_hour)
            if clean_job is not None:
                if clean_job.get("last_tool_calls") == 0:
                    # "Clean" but the agent never invoked a single tool: it
                    # could only talk, not act — a broken/missing toolset is
                    # far more likely than a legitimately idle cycle (#701).
                    alerts.append(
                        f"stage '{stage}': job reported ok for slot "
                        f"{slot_hour:02d}:00 with ZERO tool calls and no "
                        f"report — the agent could not act (broken or "
                        f"missing toolset?); treating as a stage failure"
                    )
                continue  # ran clean with real tool use — idle cycle, not a death
            alerts.append(
                f"stage '{stage}': expected report {report.name} is MISSING "
                f"(slot {slot_hour:02d}:00 + {GRACE_HOURS}h grace passed; "
                f"the job died without record or never ran)"
            )
            continue
        try:
            size = report.stat().st_size
        except OSError:
            size = 0
        if size < MIN_REPORT_BYTES:
            alerts.append(
                f"stage '{stage}': report {report.name} is suspiciously small "
                f"({size} bytes) — the cycle likely produced nothing"
            )
    return alerts


def _parse_iso(value) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value)).replace(tzinfo=None)
    except ValueError:
        return None


def check_jobs(jobs_file: Path, now: datetime) -> List[str]:
    """Alert on unhealthy evolution job records in the cron registry."""
    alerts: List[str] = []
    try:
        data = json.loads(jobs_file.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return [f"cron registry {jobs_file} unreadable: {exc}"]

    jobs = data.get("jobs", data if isinstance(data, list) else [])
    for job in jobs:
        name = str(job.get("name", ""))
        if not name.startswith("evolution-") or name in SELF_NAMES:
            continue
        if not job.get("enabled", True):
            continue

        if job.get("last_status") == "error":
            alerts.append(
                f"job '{name}': last run FAILED — {job.get('last_error') or 'no error text'}"
            )

        stale_hours = WEEKLY_STALE_HOURS if name in WEEKLY_JOBS else DAILY_STALE_HOURS
        last_run = _parse_iso(job.get("last_run_at"))
        if last_run is None:
            created = _parse_iso(job.get("created_at"))
            if created is not None and now - created <= timedelta(hours=stale_hours):
                # Freshly (re)registered job — its first slot hasn't come yet.
                # Re-registration wipes run history; alerting here is noise.
                continue
            alerts.append(f"job '{name}': has never recorded a run")
        elif now - last_run > timedelta(hours=stale_hours):
            alerts.append(
                f"job '{name}': last run {last_run.isoformat()} is stale "
                f"(>{stale_hours}h ago — interrupted without record, or scheduler down?)"
            )

        # Forward-compatible with the interrupted-job marker (issue 105):
        # a job stuck in 'running' state for many hours is dead.
        if job.get("state") == "running":
            started = _parse_iso(job.get("run_started_at"))
            if started and now - started > timedelta(hours=STUCK_RUNNING_HOURS):
                alerts.append(
                    f"job '{name}': marked running since {started.isoformat()} "
                    f"(>{STUCK_RUNNING_HOURS}h) — stuck or killed mid-run"
                )
    return alerts


def _default_runner(cmd: List[str]) -> Tuple[int, str]:
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def check_gh(
    runner: Callable[[List[str]], Tuple[int, str]] = _default_runner,
) -> List[str]:
    """Alert when gh auth is broken or API rate budget is nearly gone."""
    alerts: List[str] = []
    try:
        rc, _out = runner(["gh", "auth", "status"])
        if rc != 0:
            alerts.append("gh auth status FAILED — every pipeline stage depends on gh")
    except Exception as exc:  # noqa: BLE001 — any spawn failure is an alert
        return [f"gh unavailable: {exc}"]

    try:
        rc, out = runner(["gh", "api", "rate_limit"])
        if rc == 0:
            remaining = (
                json.loads(out).get("resources", {}).get("core", {}).get("remaining")
            )
            if remaining is not None and remaining < MIN_GH_RATE_REMAINING:
                alerts.append(
                    f"GitHub API rate budget nearly exhausted: {remaining} requests left"
                )
        else:
            alerts.append("gh api rate_limit failed — cannot verify API budget")
    except Exception as exc:  # noqa: BLE001
        alerts.append(f"gh rate-limit check failed: {exc}")
    return alerts


def _resolve_repo_dir() -> Path | None:
    """Locate the git repo to inspect for upstream lag.

    The watchdog runs as a no_agent script copied to HERMES_HOME/scripts, i.e.
    OUTSIDE the repo, so we resolve the repo explicitly: an env override, then
    the in-tree location (when run from the repo), then the common server
    install / agent-clone paths. Returns None when none is a git repo — the
    caller then skips the check silently.
    """
    candidates = [
        os.environ.get("EVOLUTION_REPO_DIR"),
        str(Path(__file__).resolve().parent.parent),  # scripts/ -> repo root (in-tree)
        "/usr/local/lib/hermes-agent",
        str(Path.home() / "hermes-agent-evolution"),
    ]
    for cand in candidates:
        if cand and (Path(cand) / ".git").exists():
            return Path(cand)
    return None


def _utcnow() -> datetime:
    """Injectable clock seam (tz-aware UTC). Overridden in tests for the grace."""
    from datetime import timezone

    return datetime.now(timezone.utc)


def check_upstream_lag(
    runner: Callable[[List[str]], Tuple[int, str]] = _default_runner,
    repo_dir: Path | None = None,
    clock: Callable[[], datetime] = _utcnow,
) -> List[str]:
    """Alert when the fork is missing the latest PUBLISHED upstream release.

    RELEASE-tracking model (2026-07-01): the fork mirrors upstream's tagged
    releases, not bleeding-edge ``upstream/main`` (which lands ~300 commits/day —
    an unwinnable chase for a heavily-customized fork). So this check no longer
    alarms on raw "behind upstream/main" (that is expected, unreleased churn).
    Instead it pings the owner when a published upstream release has NOT been
    merged into the fork within ``UPSTREAM_RELEASE_GRACE_HOURS`` — i.e. the
    release-sync is genuinely stuck, not merely a fresh tag awaiting the next run.

    Silent (returns []) when the repo can't be located, on a SHALLOW clone or one
    with no shared history with upstream (the #561 client guard — the release
    logic NEVER runs there), or on ANY gh/git/parse failure — fail-open, never a
    false alarm.
    """
    # Fail-open in FULL: a client must NEVER crash on this check, so repo
    # resolution and the shallow guard live inside the net too (a broken/absent
    # git must not raise into a client's cron).
    try:
        repo = repo_dir or _resolve_repo_dir()
        if repo is None:
            return []

        # Installer checkouts are shallow (`git clone --depth 1` in
        # scripts/install.sh / install.ps1) and share no ancestry with upstream —
        # the behind-count is a phantom there (the 2026-06 "~13000 behind" alarm on
        # every onboarded client). Shallow is the INTENDED client default;
        # release-tracking is the fork maintainer's concern (the evolution server
        # is a full clone). Skip silently BEFORE any release logic — mirrors the
        # guards in hermes_cli/banner.py & main.py. This is what keeps every
        # onboarded client silent.
        if _upstream_lag_unmeasurable(runner, repo):
            return []

        return _release_lag_alerts(runner, repo, clock)
    except Exception:  # noqa: BLE001 — this check must never crash the watchdog
        return []


def _release_lag_alerts(
    runner: Callable[[List[str]], Tuple[int, str]],
    repo: Path,
    clock: Callable[[], datetime],
) -> List[str]:
    """Core release-lag logic (wrapped by check_upstream_lag's fail-open guard)."""
    slug = _upstream_slug(runner, repo)
    if not slug:
        return []
    latest = _latest_upstream_release(runner, slug)
    if latest is None:
        return []
    tag, published = latest

    # Is the release already contained in the fork? `merge-base --is-ancestor`
    # exits 0 = ancestor (merged), 1 = not an ancestor (missing), 128/other = bad
    # ref (tag not fetched locally yet) → unmeasurable, stay silent.
    try:
        rc, _out = runner([
            "git",
            "-C",
            str(repo),
            "merge-base",
            "--is-ancestor",
            tag,
            "HEAD",
        ])
    except Exception:  # noqa: BLE001
        return []
    if rc == 0:
        return []  # latest release contained → fully current under release-tracking
    if rc != 1:
        return []  # bad/unknown ref → cannot measure, never false-alarm

    # Release genuinely not merged. Give the daily release-sync a grace window
    # before escalating, so a tag published minutes ago (sync simply hasn't run
    # yet) is not reported as "stuck".
    if not _release_older_than(published, clock, UPSTREAM_RELEASE_GRACE_HOURS):
        return []

    behind = _behind_release(runner, repo, tag)
    ahead = _count_ahead_of_release(runner, repo, tag)
    ensure_upstream_issue(
        behind=behind,
        ahead=ahead,
        tag=tag,
        runner=runner,
        gh_enabled=UPSTREAM_ISSUE_ENABLED,
    )
    return [
        f"upstream release {tag} not merged: the fork is missing the latest "
        f"published upstream release ({behind} commit(s) behind {tag}, released "
        f">{UPSTREAM_RELEASE_GRACE_HOURS}h ago). The release-sync has not landed "
        f"it — resolve the sync (see the open [UPSTREAM] issue)."
    ]


def _upstream_slug(
    runner: Callable[[List[str]], Tuple[int, str]], repo: Path
) -> str | None:
    """``owner/name`` of the ``upstream`` remote, or None (fail-open)."""
    try:
        rc, out = runner(["git", "-C", str(repo), "remote", "get-url", "upstream"])
    except Exception:  # noqa: BLE001
        return None
    if rc != 0:
        return None
    url = out.strip()
    if url.endswith(".git"):
        url = url[:-4]
    # scp-form (git@host:owner/name) vs URL-form (https://host/owner/name)
    path = url.split(":", 1)[1] if ":" in url and "//" not in url else url
    parts = [p for p in path.replace(":", "/").strip("/").split("/") if p]
    if len(parts) < 2:
        return None
    return f"{parts[-2]}/{parts[-1]}"


def _latest_upstream_release(
    runner: Callable[[List[str]], Tuple[int, str]], slug: str
) -> Tuple[str, str] | None:
    """(tagName, publishedAt) of upstream's latest GitHub release, or None."""
    try:
        rc, out = runner([
            "gh",
            "release",
            "view",
            "--repo",
            slug,
            "--json",
            "tagName,publishedAt",
        ])
    except Exception:  # noqa: BLE001 — gh missing/unauthed: fail-open
        return None
    if rc != 0 or not out.strip():
        return None
    try:
        data = json.loads(out)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    tag = str(data.get("tagName") or "").strip()
    if not tag:
        return None
    return tag, str(data.get("publishedAt") or "").strip()


def _release_older_than(
    published_iso: str, clock: Callable[[], datetime], hours: int
) -> bool:
    """True when the release was published at least ``hours`` ago.

    Fail-open SILENT: a missing timestamp (draft/odd state) or an unparseable one
    (e.g. gh changes its format) returns False, so the watchdog never nags on data
    it cannot interpret. The next sync still merges the release regardless.
    """
    if not published_iso:
        return False
    from datetime import timezone

    try:
        pub = datetime.fromisoformat(published_iso.replace("Z", "+00:00"))
    except ValueError:
        return False
    if pub.tzinfo is None:
        pub = pub.replace(tzinfo=timezone.utc)
    now = clock()
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return (now - pub) >= timedelta(hours=hours)


def _behind_release(
    runner: Callable[[List[str]], Tuple[int, str]], repo: Path, tag: str
) -> int:
    """Commits in the release tag not yet in the fork (best-effort, 0 on error)."""
    return _rev_list_count(runner, repo, f"HEAD..{tag}")


def _count_ahead_of_release(
    runner: Callable[[List[str]], Tuple[int, str]], repo: Path, tag: str
) -> int:
    """Commits the fork has that the release tag does not (best-effort, 0)."""
    return _rev_list_count(runner, repo, f"{tag}..HEAD")


def _rev_list_count(
    runner: Callable[[List[str]], Tuple[int, str]], repo: Path, rng: str
) -> int:
    try:
        rc, out = runner(["git", "-C", str(repo), "rev-list", "--count", rng])
    except Exception:  # noqa: BLE001
        return 0
    if rc != 0:
        return 0
    try:
        return int(out.strip().split()[0])
    except (ValueError, IndexError):
        return 0


def ensure_upstream_issue(
    behind: int,
    ahead: int,
    tag: str = "",
    runner: Callable[[List[str]], Tuple[int, str]] = _default_runner,
    gh_enabled: bool = True,
) -> str | None:
    """Idempotently ensure a GitHub ``[UPSTREAM]`` tracking issue exists.

    Called only on a REAL escalation (full clone, a published upstream release
    unmerged past the grace window — the #561 shallow case never reaches here).
    The owner had to open issue #562 by hand; this closes that gap.

    Idempotency key: an OPEN issue whose title starts with ``UPSTREAM_ISSUE_PREFIX``.
    If one exists we do NOT create a duplicate (de-duped / edge-triggered on the
    issue's own existence — no daily spam). If none exists we create one naming the
    missing release tag and the real behind/ahead counts.

    ALL gh interaction goes through the injectable ``runner`` seam, so this is
    unit-testable without network. FAIL-OPEN throughout: gh missing/unauthed, a
    failed search, or any spawn error → return None and do nothing (the text
    alert from ``check_upstream_lag`` still informs the owner). Returns a short
    human confirmation string when it actually created an issue, else None.
    """
    if not gh_enabled:
        return None

    # 1) Look for an existing open [UPSTREAM] issue (the idempotency key).
    try:
        rc, out = runner([
            "gh",
            "issue",
            "list",
            "--search",
            f"{UPSTREAM_ISSUE_PREFIX} in:title",
            "--state",
            "open",
            "--json",
            "number,title",
        ])
    except Exception:  # noqa: BLE001 — gh missing/spawn failure: fail-open
        return None
    if rc != 0:
        # Search failed: do NOT blind-create (would risk duplicates/spam).
        return None
    try:
        issues = json.loads(out) if out.strip() else []
    except ValueError:
        return None
    for issue in issues if isinstance(issues, list) else []:
        title = str(issue.get("title", "")) if isinstance(issue, dict) else ""
        if title.startswith(UPSTREAM_ISSUE_PREFIX):
            return None  # already tracked — never duplicate

    # 2) None exists → create one naming the missing release + real counts.
    rel = f"release `{tag}`" if tag else "the latest upstream release"
    title = (
        f"{UPSTREAM_ISSUE_PREFIX} Release not merged: fork ~{behind} commit(s) "
        f"behind {('`' + tag + '`') if tag else 'the latest upstream release'} "
        f"(owner review)"
    )
    body = (
        f"The autonomous release-sync could not land {rel}: the fork is ~{behind} "
        f"commit(s) behind it and ~{ahead} commit(s) ahead.\n\n"
        f"This issue was filed automatically by the evolution watchdog so the "
        f"missing release is visible. Resolve by merging the release tag into the "
        f"fork (owner review of any conflicts), then close this issue.\n"
    )
    try:
        rc, _out = runner(["gh", "issue", "create", "--title", title, "--body", body])
    except Exception:  # noqa: BLE001 — fail-open on create spawn failure
        return None
    if rc != 0:
        return None
    return f"filed {UPSTREAM_ISSUE_PREFIX} tracking issue ({behind} behind)"


def _upstream_lag_unmeasurable(
    runner: Callable[[List[str]], Tuple[int, str]], repo: Path
) -> bool:
    """True when ``HEAD..upstream/main`` can't yield a meaningful behind-count.

    Two independent signals, either of which makes the numeric count a phantom:
      1. shallow repo — ``git rev-parse --is-shallow-repository`` == "true"
         (the `git clone --depth 1` installer default);
      2. no shared history — ``git merge-base HEAD upstream/main`` exits non-zero
         with EMPTY stdout (HEAD and upstream share no common ancestor, e.g. a
         grafted clone, even when the shallow flag is unset).

    Best-effort and FAIL-OPEN: any spawn error or inconclusive result returns
    False, so a normal full clone proceeds to the real rev-list count exactly as
    before — this can never make the check worse than today.
    """
    try:
        rc, out = runner([
            "git",
            "-C",
            str(repo),
            "rev-parse",
            "--is-shallow-repository",
        ])
        if rc == 0 and out.strip() == "true":
            return True
    except Exception:  # noqa: BLE001 — inconclusive probe: don't block the real check
        return False

    try:
        rc, out = runner([
            "git",
            "-C",
            str(repo),
            "merge-base",
            "HEAD",
            "upstream/main",
        ])
    except Exception:  # noqa: BLE001
        return False
    # No common ancestor: git exits non-zero with NOTHING on stdout. A non-zero
    # exit WITH output (e.g. "fatal: bad revision 'upstream/main'" when the remote
    # is merely missing) is the unrelated missing-remote case — leave that to the
    # rev-list step, which already fails silently, so we don't turn a missing
    # remote into a spurious shallow skip.
    if rc != 0 and not out.strip():
        return True
    return False


def check_runtime_divergence(
    runner: Callable[[List[str]], Tuple[int, str]] = _default_runner,
    repo_dir: Path | None = None,
) -> List[str]:
    """Alert when the local runtime checkout has diverged from ``origin/main``.

    THE SILENT FREEZE (root cause of the stalled nightly self-update): the
    runtime checkout self-updates with ``git pull --ff-only``. When the evolution
    pipeline (or a contributor) leaves LOCAL commits on the tracking branch that
    later squash-merge upstream under a DIFFERENT SHA, local HEAD diverges from
    ``origin/main``; ff-only can no longer fast-forward, so the nightly update
    silently no-ops and the box freezes on an old revision with NO signal.

    We DETECT + ALERT only — never auto-reset/auto-heal (that risks losing the
    local commits). The fix is making the freeze loud via the owner's channel.

    DIVERGED (the high-confidence signal we alert on):
      * ``rev-list --count origin/main..HEAD`` > 0  (local commits not on origin)
      * AND ``merge-base --is-ancestor HEAD origin/main`` is FALSE (HEAD is not
        reachable from origin/main → a plain ff-only pull CANNOT advance).
    The is-ancestor probe is authoritative: if HEAD is still an ancestor of
    origin/main, ff-only would advance, so it is NOT a freeze even if rev-list
    reports stray local commits.

    STALE (behind but ff-able) is deliberately NOT alerted here: a healthy box
    that simply hasn't pulled yet today is behind-but-fast-forwardable, and
    alerting on it would storm every morning. The upstream-lag check already
    covers the genuine "sync is stuck" case for the fork maintainer.

    FAIL-OPEN: repo unresolved, any git/spawn error, or unparseable output →
    return [] (behaves exactly as today, never a false alarm).
    """
    repo = repo_dir or _resolve_repo_dir()
    if repo is None:
        return []

    try:
        rc_anc, _out = runner([
            "git",
            "-C",
            str(repo),
            "merge-base",
            "--is-ancestor",
            "HEAD",
            "origin/main",
        ])
    except Exception:  # noqa: BLE001 — spawn failure: fail-open
        return []
    # rc 0 == HEAD IS an ancestor of origin/main → ff-able → not frozen.
    # rc 1 == not an ancestor → potential divergence. Any other rc (e.g. 128 for
    # a bad repo/ref) is inconclusive → fail-open silent.
    if rc_anc == 0:
        return []
    if rc_anc != 1:
        return []

    try:
        rc_ahead, out_ahead = runner([
            "git",
            "-C",
            str(repo),
            "rev-list",
            "--count",
            "origin/main..HEAD",
        ])
    except Exception:  # noqa: BLE001
        return []
    if rc_ahead != 0:
        return []
    try:
        local_commits = int(out_ahead.strip().split()[0])
    except (ValueError, IndexError):
        return []
    if local_commits <= 0:
        return []

    behind = 0
    try:
        rc_behind, out_behind = runner([
            "git",
            "-C",
            str(repo),
            "rev-list",
            "--count",
            "HEAD..origin/main",
        ])
        if rc_behind == 0:
            behind = int(out_behind.strip().split()[0])
    except (Exception, ValueError, IndexError):  # noqa: BLE001 — count is cosmetic
        behind = 0

    plural = "s" if local_commits != 1 else ""
    return [
        f"runtime checkout diverged from origin/main by {local_commits} local "
        f"commit{plural} (origin is {behind} ahead) — nightly self-update is "
        f"frozen (can't fast-forward); reconcile to origin/main."
    ]


def check_health(evolution_dir: Path) -> List[str]:
    """Alert when the longitudinal health sidecar reports degraded calibration.

    evolution_metrics writes ``evolution-health.txt`` ending in ``| healthy`` when
    fine, or ``| <FLAGS>`` (LOW_SUCCESS / LOW_SELECTION_EFFICIENCY) when the
    pipeline is selecting more than it can land / rarely merging. This is what
    makes the measurement spine ACTIONABLE instead of write-only: the owner gets
    pinged when the pipeline's own health degrades. Silent when healthy or when
    there is no sidecar yet (the stage-report checks already cover 'funnel didn't
    run')."""
    try:
        line = (
            (evolution_dir / "evolution-health.txt").read_text(encoding="utf-8").strip()
        )
    except OSError:
        return []
    if not line or line.endswith("| healthy"):
        return []
    return [f"pipeline health degraded: {line}"]


def check_realized_impact(evolution_dir: Path) -> List[str]:
    """Alert when the post-merge realized-impact sidecar reports blind evolution.

    evolution_realized_impact writes ``realized-impact.txt`` ending in
    ``| healthy`` when merged changes land real value, or ``| <FLAGS>``
    (REALIZED_IMPACT_LOW / REALIZED_RATE_LOW / UNVERIFIED_BACKLOG) when the agent
    is shipping plausible-but-useless code or the verification step has stopped
    running. This is the loop that stops the agent from optimizing a predicted
    impact it never checks against reality. Silent when healthy or absent."""
    try:
        line = (
            (evolution_dir / "realized-impact.txt").read_text(encoding="utf-8").strip()
        )
    except OSError:
        return []
    if not line or line.endswith("| healthy"):
        return []
    return [f"realized-impact degraded: {line}"]


def check_analysis_integrity(evolution_dir: Path) -> List[str]:
    """Alert when the latest analysis cycle's self-reported selection budget is
    illegal or overspent (PR #519's effort-budget contract — the agent once wrote
    max_total_effort=2.0, neither 1.5 nor 3.0), OR when an ``already-exists``
    rejection cited repo paths that do not exist (the #83 fabricated-close class —
    needs the repo, resolved via _resolve_repo_dir). Silent when clean, when there
    is no dated analysis report yet, or when the audit module is unavailable (the
    scheduler installs evolution_*.py alongside this script, so the sibling import
    resolves at runtime; the guard keeps unit imports safe)."""
    try:
        from evolution_analysis_audit import audit_latest
    except ImportError:
        return []
    return [
        f"analysis selection integrity: {v}"
        for v in audit_latest(evolution_dir, _resolve_repo_dir())
    ]


# ---------------------------------------------------------------------------
# Reality reconciliation — never forward a metric-derived diagnosis that
# GitHub reality contradicts.
#
# The class of failure this catches: the pipeline measures ITSELF, so when its
# self-measurement is wrong it draws (and escalates) a wrong conclusion. The
# 2026-07 incident: the funnel counted `merged` only from the integration
# stage's self-report, missing the owner's manual merges — so health screamed
# "integration stuck / picks more than it can land" for 7 days while GitHub
# showed 15 evolution PRs actually merging. PR #885 anchored `merged` to GitHub
# so the sidecar is now truthful, but a self-measuring system needs a standing
# immune response: if a FUTURE metric regression (or a mis-dated stage report,
# #667) ever makes the health line claim a merge/selection stall while GitHub
# shows evolution PRs merging, that is an INSTRUMENTATION fault, not a pipeline
# stall. Relabel it so the owner gets the honest diagnosis instead of chasing a
# phantom — and so the pipeline can act on the right (funnel/#667) issue.
#
# Fail-open + no-mask: any gh failure, or GitHub agreeing there are no merges
# (a real stall), leaves the alerts EXACTLY as-is. Reconciliation can only ever
# RELABEL a merge/selection alert that reality demonstrably contradicts; it
# never suppresses one.
#
# SCOPE — only MERGED_ZERO. It is the sole health flag whose predicate is a
# COUNT claim ("zero merges this window") that a nonzero GitHub count genuinely
# contradicts. LOW_SUCCESS and LOW_SELECTION_EFFICIENCY are RATE/RATIO claims
# (fraction of cycles that landed a merge; distinct-selected-that-landed /
# distinct-selected) — a single existing merge does NOT refute a low rate
# (select 100, land 1 → ratio 0.01 is a TRUE degradation that coexists with
# count>=1). Relabeling those on a mere count would VIOLATE no-mask and hide the
# very quality signal the watchdog exists to protect, so they are deliberately
# excluded. A sound ratio reconciliation (GitHub-derived merged/selected over
# the same window) is a possible future enhancement, not needed for correctness.
# ---------------------------------------------------------------------------

_DIVERGEABLE_FLAGS = ("MERGED_ZERO",)
_MIN_HEALTHY_MERGES = 1  # >=1 evolution/issue-* issue merged == the zero claim is false
_REALITY_WINDOW_DAYS = 7


def recent_merged_evolution_issue_count(
    now: datetime,
    runner: Callable[[List[str]], Tuple[int, str]] = _default_runner,
    window_days: int = _REALITY_WINDOW_DAYS,
    branch_prefix: str = "evolution/issue-",
) -> int | None:
    """Distinct evolution issues merged on GitHub within the last ``window_days``.

    Returns None on any gh failure so the caller can fall open (never suppress a
    real alert on a reconciliation error). Distinct issue numbers, so increment
    PRs (issue-798-inc1/2/3) count once."""
    try:
        rc, out = runner([
            "gh",
            "pr",
            "list",
            "--state",
            "merged",
            "--json",
            "headRefName,mergedAt",
            "--limit",
            "100",
        ])
        if rc != 0:
            return None
        prs = json.loads(out)
    except Exception:  # noqa: BLE001 — any gh/parse failure → fall open
        return None
    if not isinstance(prs, list):
        return None
    cutoff = (now - timedelta(days=window_days)).strftime("%Y-%m-%d")
    pat = re.compile(re.escape(branch_prefix) + r"(\d+)")
    ids: set[int] = set()
    for pr in prs:
        if not isinstance(pr, dict):
            continue
        merged_day = str(pr.get("mergedAt", "") or "")[:10]  # ISO dates sort lexically
        m = pat.match(str(pr.get("headRefName", "")))
        if m and merged_day and merged_day >= cutoff:
            ids.add(int(m.group(1)))
    return len(ids)


def reconcile_health_with_reality(
    health_alerts: List[str],
    now: datetime,
    runner: Callable[[List[str]], Tuple[int, str]] = _default_runner,
    window_days: int = _REALITY_WINDOW_DAYS,
) -> List[str]:
    """Relabel a MERGED_ZERO health alert that GitHub reality contradicts.

    MERGED_ZERO claims the funnel recorded zero merges for a run of cycles. If
    GitHub shows evolution issues actually merging in the window, that COUNT
    claim is false — replace the alert with a single honest METRIC_DIVERGENCE
    line (stable flag tail, so edge-triggering keys on it without re-screaming
    the drifting merge count). Only MERGED_ZERO is in scope (see the block
    above): rate/ratio flags are deliberately NOT relabeled on a count, to
    preserve no-mask. Fail-open: gh unknown, or GitHub agreeing there were no
    merges, leaves ``health_alerts`` untouched."""
    if not any(any(f in a for f in _DIVERGEABLE_FLAGS) for a in health_alerts):
        return health_alerts
    merged = recent_merged_evolution_issue_count(now, runner, window_days)
    if merged is None or merged < _MIN_HEALTHY_MERGES:
        return health_alerts  # gh unknown OR reality agrees it's stalled → keep as-is
    divergence = (
        f"funnel reports zero merges, but GitHub shows {merged} evolution/issue-* "
        f"issue(s) merged in the last {window_days}d "
        "| METRIC_DIVERGENCE: instrumentation suspect (funnel / #667), NOT the "
        "pipeline — verify the metric, auto-clears once the funnel backfills"
    )
    out: List[str] = []
    inserted = False
    for a in health_alerts:
        if any(f in a for f in _DIVERGEABLE_FLAGS):
            if not inserted:  # collapse all diverging flags into one honest line
                out.append(divergence)
                inserted = True
        else:
            out.append(a)
    return out


# ---------------------------------------------------------------------------
# Edge-triggering for the steady-state HEALTH alerts.
#
# WHY: the pipeline-health checks (check_health / check_realized_impact /
# check_analysis_integrity) re-emit the SAME alert on EVERY cron run while a
# known, already-throttled condition persists (e.g. selection_efficiency=11%,
# self-corrected by PR #519's deterministic effort_budget). Re-screaming a
# steady condition daily is pure fatigue — it adds no information.
#
# WHAT we do: alert on TRANSITIONS, not on steady state. We emit when
#   • a NEW flag/condition appears that wasn't present last run,
#   • a condition WORSENS (a new/harsher flag, or an embedded counter such as
#     `MERGED_ZERO x3 -> x5` grows — both change the flag tail = the signature),
#   • a condition CLEARS (recovery — announced once),
#   • a condition has persisted UNCHANGED for >= EDGE_COOLDOWN_DAYS days
#     (a single "still unresolved" nudge so it is never silently forgotten).
# We SUPPRESS only the verbatim repeat of an already-reported, non-worsening
# condition within the cooldown window.
#
# NO-MASK SAFETY PROPERTY: suppression keys on a *condition signature* (the
# sorted flag tails), so any new fault, any worsening, and any new distinct
# flag changes the signature and emits immediately. Suppression can ONLY hide a
# byte-for-byte-equivalent condition we already reported. Operational alerts
# (stage reports, jobs, gh, upstream-lag from #561) never pass through here.
#
# FAIL-OPEN CONTRACT: every state read/write is best-effort. A missing,
# unreadable, or corrupt state file means "unknown previous state" → we emit
# exactly as the watchdog does today. A write failure is swallowed (never
# crashes the run, never suppresses the current alert). Edge-triggering can
# therefore only ever REDUCE noise, never mask a fault.
#
# KNOWN BOUND (acceptable by design): the signature is the set of flag tails,
# so a *worsening WITHIN a single binary flag* (e.g. selection_efficiency
# 11% → 1%, both below the one LOW_SELECTION_EFFICIENCY threshold the sidecars
# expose) does not change the signature and is suppressed until either a new
# flag joins or the EDGE_COOLDOWN_DAYS re-reminder fires. The sidecars have no
# WARN/CRITICAL sub-tiers to cross, so there is no finer "worse threshold" to
# key on today; if one is added, extend the tail to include it. The cooldown
# nudge is the backstop that guarantees no condition is silent forever.
# ---------------------------------------------------------------------------


def health_signature(health_alerts: List[str]) -> str:
    """Stable, count-aware condition key for a set of health alerts.

    Keys on the FLAG TAIL of each alert (the text after the final ``|``), not
    the full descriptive line: the metrics body carries run-to-run counts
    (``cycles_active``, ``selected=…``) that drift even when the condition is
    unchanged — including the body would make every run look "new" and nothing
    would ever be suppressed. Embedded severity counters that live in the tail
    (``MERGED_ZERO x5``) DO change the signature, so a worsening still trips it.

    Order-independent (alerts are sorted) and returns ``""`` for no condition
    (healthy), which is the recovery sentinel.
    """
    tails: List[str] = []
    for alert in health_alerts:
        # The flag tail is everything after the last "| " separator that the
        # sidecars use to terminate the metrics body. When there is no such
        # separator (e.g. analysis-integrity alerts), the whole string IS the
        # condition.
        tail = alert.rsplit("| ", 1)[-1].strip() if "| " in alert else alert.strip()
        tails.append(tail)
    return "\n".join(sorted(tails))


def load_alert_state(state_path: Path) -> dict | None:
    """Read the persisted alert state. FAIL-OPEN: any miss/IO/parse error or a
    structurally invalid payload returns None (== unknown previous state)."""
    try:
        data = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or "signature" not in data:
        return None
    return data


def save_alert_state(
    state_path: Path, signature: str, last_emitted_at: datetime
) -> None:
    """Persist the current signature + last-emitted timestamp. FAIL-OPEN: a
    write failure is swallowed — it must never crash the run nor (by raising)
    suppress an alert the caller already decided to emit."""
    try:
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(
            json.dumps({
                "signature": signature,
                "last_emitted_at": last_emitted_at.isoformat(),
            }),
            encoding="utf-8",
        )
    except OSError:
        return


def apply_edge_trigger(
    health_alerts: List[str],
    state_path: Path,
    now: datetime,
    cooldown_days: int = EDGE_COOLDOWN_DAYS,
) -> List[str]:
    """Decide which HEALTH alerts to actually emit this run, and persist state.

    Returns the alerts to print (possibly a single recovery/reminder line in
    place of the raw alerts). See the design block above for the full rules.
    """
    sig = health_signature(health_alerts)
    prev = load_alert_state(state_path)
    prev_sig = prev.get("signature") if prev else None
    prev_ts = _parse_iso(prev.get("last_emitted_at")) if prev else None

    # --- Transition: the condition changed (or we have no prior state) -------
    if sig != prev_sig:
        if sig == "":
            # Cleared. Announce recovery exactly once IF we actually had a prior
            # non-empty condition on record. (prev_sig is None on a fresh/corrupt
            # state with nothing wrong → nothing to recover, stay silent.)
            if prev_sig:
                save_alert_state(state_path, "", now)
                return [
                    "pipeline health RECOVERED: previously-flagged condition has "
                    "cleared (no health flags this run)"
                ]
            # Fail-open with no condition: record the healthy baseline, emit nothing.
            save_alert_state(state_path, "", now)
            return []
        # New / worsening / changed condition (or fail-open unknown prior) → emit.
        save_alert_state(state_path, sig, now)
        return health_alerts

    # --- Steady state: signature identical to what we last saw ---------------
    if sig == "":
        # Still healthy — nothing to say, keep the baseline fresh.
        save_alert_state(state_path, "", now)
        return []

    # Identical non-empty condition. Suppress unless the cooldown elapsed.
    if prev_ts is not None and now - prev_ts >= timedelta(days=cooldown_days):
        # Long-cooldown re-reminder: never let a real fault go silent forever.
        save_alert_state(state_path, sig, now)  # reset the clock
        days = (now - prev_ts).days
        return [
            f"still unresolved after {days}d (no change since last alert) — {a}"
            for a in health_alerts
        ]
    # Verbatim repeat within cooldown → suppress. Do NOT refresh the timestamp,
    # so the re-reminder fires relative to the LAST real emission.
    return []


def main() -> int:
    hermes_home = Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes")))
    evolution_dir = Path(
        os.environ.get(
            "EVOLUTION_PROFILE_DIR",
            str(hermes_home / "evolution"),
        )
    )
    jobs_file = hermes_home / "cron" / "jobs.json"
    # Stage reports are dated in Hermes' CONFIGURED timezone (hermes_time.now),
    # which may differ from the server's local zone — around midnight a naive
    # datetime.now() would then look for the wrong report date. The scheduler
    # runs this script from HERMES_HOME/scripts (outside the repo's sys.path),
    # so hermes_time may not be importable — fall back to server-local wall
    # time, which is identical whenever no explicit timezone is configured.
    try:
        from hermes_time import now as _hermes_now

        now = _hermes_now().replace(tzinfo=None)
    except ImportError:
        now = datetime.now()

    # Operational alerts: acute infra/scheduler/sync failures. These are ALWAYS
    # emitted every run — they are not steady-state pipeline-health conditions
    # and must never be edge-suppressed (a broken gh or a stuck upstream-sync is
    # actionable every single day until fixed). The #561 upstream-lag guard is
    # untouched: its own shallow/no-shared-history checks decide if it speaks.
    operational: List[str] = []
    operational += check_stage_reports(evolution_dir, now, jobs_file)
    operational += check_jobs(jobs_file, now)
    operational += check_gh()
    operational += check_upstream_lag()

    # Pipeline-HEALTH alerts: steady-state calibration/quality conditions that
    # self-correct over time (effort_budget throttle) and re-fire identically
    # every run. Only THESE pass through the edge-trigger (transitions, not
    # steady state) — see the design block above. Fail-open: on any state error
    # the layer emits exactly as the watchdog does today.
    #
    # check_runtime_divergence rides this edge-trigger path too: a diverged
    # runtime checkout is a steady condition that persists UNCHANGED until the
    # owner reconciles it, so re-screaming it every run is pure fatigue. The
    # signature keys on the alert text (no '|' tail), so it emits on first
    # sighting, on any change (commit count moves), and on the cooldown nudge —
    # but suppresses the verbatim daily repeat. No-mask + fail-open preserved.
    health: List[str] = []
    health += check_runtime_divergence()
    # Reality-reconcile the merge/selection health line before it can page the
    # owner: a metric that claims a stall while GitHub shows evolution PRs
    # merging is an instrumentation fault, not a pipeline one (see the
    # reconcile_health_with_reality block). Fail-open — never suppresses a real
    # degradation, only relabels one reality demonstrably contradicts.
    health += reconcile_health_with_reality(check_health(evolution_dir), now)
    health += check_realized_impact(evolution_dir)
    health += check_analysis_integrity(evolution_dir)
    health = apply_edge_trigger(health, evolution_dir / ALERT_STATE_FILENAME, now)

    alerts: List[str] = operational + health

    if alerts:
        print("🐶 Evolution watchdog — pipeline anomalies detected:")
        for a in alerts:
            print(f"  • {a}")
        print(
            f"\n(checked {len(STAGES)} stage reports, cron registry, gh access "
            f"at {now.isoformat(timespec='seconds')})"
        )
    # Empty stdout = healthy = silent run (scheduler delivers nothing).
    return 0


if __name__ == "__main__":
    sys.exit(main())
